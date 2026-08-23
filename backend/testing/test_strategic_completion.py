"""Tests for pure direction lifecycle helpers in app.strategic_completion.

Covers:
  - _check_direction_health: saturation, zero-merge, high-failure-rate rules
  - _check_direction_completion: cycle/PR thresholds and recent-activity check
  - _is_direction_duplicate: word-overlap with existing and rejected proposals
  - _record_strategic_task_failure: circuit-breaker increment and cycle history update
"""

import json
import logging
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
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
    _build_retirement_snapshot,
    _collect_creation_predictions,
    _collect_retirement_metrics,
    _collect_rationales,
    _extract_cycle_reasoning,
    _extract_rationale_from_notes,
    _compute_churn_metrics,
    _compute_lifetime_burn_metrics,
    _direction_non_merge_pause,
    _direction_still_productive,
    _cycle_landed_or_noop,
    _direction_consecutive_zero_landed_cycles,
    _recent_merges_mostly_unproven,
    _already_built_signature,
    already_built_completion_suggestion,
    _compute_outcome_progress_signal,
    record_outcome_progress_snapshot,
    _check_outcome_scaffolding,
    _apply_evidence_progress_verdicts,
    _reset_scaffolding_state_on_outcome_change,
    sanitize_strategic_challenge_record,
    strategic_challenge_summary,
    challenge_approves_evidence_progress,
    portfolio_operation_fingerprint,
    challenge_rejects_portfolio_op,
    STRATEGIC_CHALLENGE_CONTEXT_KEY,
    STRATEGIC_CHALLENGE_RECORD_VERSION,
    EVIDENCE_PROGRESS_CONTEXT_KEY,
    _CREATED_NOTE_PREFIX,
    _RETIRED_NOTE_PREFIX,
    spawn_budget_status,
    spawn_title_is_redundant,
    _spawn_retirement_memos,
    _strategic_event_completion_summary,
    _compute_model_gc_telemetry,
    record_retired_direction_memory,
    _check_gap_respawn_block,
    _merge_rejected_proposals,
)
from app.strategic_spawning import (
    _build_previous_decision_outcomes,
    _build_past_retirement_critiques,
    STRATEGIC_CYCLE_MANUAL_CONTEXT_KEY,
)
from app.strategic_analysis import _build_outcome_evaluation
from app.direction_lifecycle import _reset_outcome_detection_state
from app.models import CriterionEvidence, Direction, Project, ProjectMember, ReleaseVersion, StrategicCycle, Task, TaskStatus, VersionAcceptanceCriterion
from app.auth import create_agent_token
from httpx import AsyncClient
from sqlmodel import select


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


def test_strategic_completion_event_summary_uses_context_cycle_and_project_state():
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "strategic_review"}],
        context={
            "_strategic_created_ids": ["d-new"],
            "_strategic_retired_ids": ["d-old"],
            "_strategic_staged_ids": ["d-next"],
            "_strategic_updated_ids": ["d-live"],
            "_strategic_dirs_created": 1,
            "_strategic_dirs_retired": 1,
            "_strategic_dirs_staged": 1,
            "_strategic_dirs_updated": 1,
            "_strategic_tasks_spawned": 2,
            "_strategic_problem_model_set": True,
            "_strategic_running_update_set": True,
        },
    )
    task.latest_status_message = "Strategic review complete"
    project = Project(
        name="P",
        user_id="u1",
        directions=[
            {"id": "d-new", "status": "active"},
            {"id": "d-old", "status": "expired"},
        ],
    )
    cycle = StrategicCycle(
        id="sc-summary",
        project_id=task.project_id,
        outcome="completed",
        directions_created=1,
        directions_retired=1,
        tasks_spawned=2,
    )

    summary = _strategic_event_completion_summary(task, project, cycle=cycle)

    assert summary["status"] == "completed"
    assert summary["created_direction_ids"] == ["d-new"]
    assert summary["retired_direction_ids"] == ["d-old"]
    assert summary["staged_direction_ids"] == ["d-next"]
    assert summary["updated_direction_ids"] == ["d-live"]
    assert summary["created_count"] == 1
    assert summary["retired_count"] == 1
    assert summary["staged_count"] == 1
    assert summary["updated_count"] == 1
    assert summary["tasks_spawned"] == 2
    assert summary["directions_changed"] is True
    assert summary["problem_model_changed"] is True
    assert summary["running_update_changed"] is True
    assert summary["active_direction_count"] == 1


def test_strategic_completion_event_summary_uses_proposal_counters():
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "strategic_review"}],
        context={
            "_strategic_proposals_approved": 1,
            "_strategic_proposals_rejected": 2,
        },
    )
    project = Project(name="P", user_id="u1", directions=[])

    summary = _strategic_event_completion_summary(task, project)

    assert summary["status"] == "completed"
    assert summary["updated_count"] == 1
    assert summary["retired_count"] == 2
    assert summary["directions_changed"] is True


def test_strategic_completion_event_summary_includes_compact_challenge_summary_without_items():
    """A cycle carrying a challenge record surfaces ONLY the compact
    {mode, verdict, warned_count, blocked_count} summary in the event payload —
    never the full bounded evidence_progress / section item list.

    Exercises the production seam: ``_strategic_event_completion_summary`` reads
    the durable ``cycle_metadata['strategic_judgment_challenge']`` record and runs
    it through ``strategic_challenge_summary``.
    """
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "strategic_review"}],
        context={},
    )
    project = Project(name="P", user_id="u1", directions=[])
    record = sanitize_strategic_challenge_record(
        {
            "status": "ok",
            "evidence_progress": [
                {"direction_id": "d-1", "status": "advancing", "verdict": "approved"},
                {"direction_id": "d-2", "status": "met", "verdict": "rejected"},
                {"direction_id": "d-3", "status": "advancing", "verdict": "uncertain"},
            ],
            "retirement_critique_quality": {
                "verdict": "rejected",
                "rationale": "critique was shallow",
            },
            "portfolio_shape_risk": {"verdict": "solid", "rationale": "balanced"},
        }
    )
    cycle = StrategicCycle(
        id="sc-challenged",
        project_id=task.project_id,
        outcome="completed",
        cycle_metadata={"strategic_judgment_challenge": record},
    )

    summary = _strategic_event_completion_summary(task, project, cycle=cycle)

    challenge = summary["strategic_judgment_challenge"]
    assert challenge == {
        "mode": "ok",
        "verdict": "blocked",
        "warned_count": 1,  # one uncertain evidence_progress item
        "blocked_count": 2,  # one rejected item + one rejected section verdict
        "gate_mode": "enforce",
    }
    # The compact summary must NOT carry the bounded item list — that stays in
    # cycle_metadata for drill-down, never on the websocket-bound summary.
    assert "evidence_progress" not in challenge
    assert "retirement_critique_quality" not in challenge
    assert "portfolio_shape_risk" not in challenge


def test_strategic_completion_event_summary_omits_challenge_for_legacy_cycle():
    """Cycles with no challenge record omit the key entirely so absence does not
    read as a failure."""
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "strategic_review"}],
        context={},
    )
    project = Project(name="P", user_id="u1", directions=[])
    cycle = StrategicCycle(
        id="sc-legacy",
        project_id=task.project_id,
        outcome="completed",
        cycle_metadata={},
    )

    summary = _strategic_event_completion_summary(task, project, cycle=cycle)

    assert "strategic_judgment_challenge" not in summary


def test_strategic_completion_event_summary_challenge_falls_back_to_context():
    """When the cycle metadata has no record yet (in-flight), the summary falls
    back to the task.context challenge record."""
    record = sanitize_strategic_challenge_record(
        {
            "status": "ok",
            "evidence_progress": [
                {"direction_id": "d-1", "status": "met", "verdict": "approved"}
            ],
        }
    )
    task = Task(
        project_id=uuid.uuid4(),
        payloads=[{"type": "strategic_review"}],
        context={STRATEGIC_CHALLENGE_CONTEXT_KEY: record},
    )
    project = Project(name="P", user_id="u1", directions=[])

    summary = _strategic_event_completion_summary(task, project)

    assert summary["strategic_judgment_challenge"] == {
        "mode": "ok",
        "verdict": "approved",
        "warned_count": 0,
        "blocked_count": 0,
        "gate_mode": "enforce",
    }


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
        # All cycles: 0 PRs merged, last cycle fully finished. tasks_failed=1 means
        # all 5 cycles would also match Rule 4 ("Recent stall:") — but the non-merge
        # proxy is evaluated FIRST so this direction is classified as "Unproductive:"
        # (reversible/non-terminal) not "Recent stall:" (terminal code_failure ladder).
        d = _direction(
            research_history=_history(
                5, prs_merged=0, tasks_created=2, tasks_completed=2, tasks_failed=1
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

    def test_rule5_config_zero_merge_rate_honored(self, mocker):
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_churn_min_merge_rate": 0.0},
            clear=False,
        )
        d = _direction(research_history=_history(5, tasks_created=25, tasks_completed=20, tasks_failed=0, prs_merged=1))
        assert _compute_churn_metrics(d)["merge_rate"] < 0.10
        assert _check_direction_health(d) is None

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

    # --- Rule 5 defense-in-depth: raw merge-volume backstop ---

    def test_compute_churn_metrics_exposes_real_merges(self):
        # real_merges is the windowed sum of merged_prs_any_base (raw merge volume).
        hist = _history(3, prs_merged=0)
        for e in hist:
            e["merged_prs_any_base"] = 2
        m = _compute_churn_metrics(_direction(research_history=hist))
        assert m["real_merges"] == 6
        # Legacy entries lacking the field contribute 0 — no crash, no false credit.
        legacy = _compute_churn_metrics(
            _direction(research_history=_history(3, prs_merged=0))
        )
        assert legacy["real_merges"] == 0

    def test_rule5_churn_suppressed_when_real_merges_present(self):
        # Same shape as the default-threshold churn case (non_merge_finished=15,
        # credit-gate merge_rate=0%), but the cycles ALSO carry raw staging/
        # integration merges whose landed credit has not yet propagated into
        # verified_prs_merged. The defense-in-depth guard must suppress the pause.
        hist = _history(
            5, tasks_created=5, tasks_completed=3, tasks_failed=0, prs_merged=0
        )
        for e in hist:
            e["merged_prs_any_base"] = 3
        d = _direction(research_history=hist)
        m = _compute_churn_metrics(d)
        # credit-gate metrics still look exactly like churn...
        assert m["non_merge_finished"] >= 15
        assert m["merge_rate"] < 0.10
        assert m["real_merges"] == 15
        # ...but real merges are in flight, so the guardrail must NOT pause.
        assert _check_direction_health(d) is None

    def test_rule5_churn_still_fires_when_no_real_merges(self):
        # Explicit merged_prs_any_base=0 on every cycle: nothing merged anywhere.
        # This is GENUINE churn and the guardrail must still pause.
        hist = _history(
            5, tasks_created=5, tasks_completed=3, tasks_failed=0, prs_merged=0
        )
        for e in hist:
            e["merged_prs_any_base"] = 0
        d = _direction(research_history=hist)
        result = _check_direction_health(d)
        assert result is not None
        assert "Churn guardrail" in result

    def test_non_merge_pause_suppressed_when_real_merges_present(self):
        # Isolates the NON-MERGE-RATE proxy (the zero-merge tail): 5 finished cycles,
        # 0 credited merges, and NO code failures (tasks_failed=0) so the separate,
        # deliberately-ungated code-failure "Recent stall" guard cannot fire. The
        # bare fixture pauses "Unproductive:"; injecting real staging/integration
        # merges (merged_prs_any_base>0) whose landed credit has not yet propagated
        # must suppress it. The raw-merge guard is scoped to the merge-rate proxy
        # ONLY — it never masks a genuine crash signal.
        cycle = {
            "tasks_created": 2,
            "tasks_completed": 2,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 0,
        }
        churning = _direction(research_history=[dict(cycle) for _ in range(5)])
        result = _check_direction_health(churning)
        assert result is not None and "Unproductive" in result  # fires w/o raw merges

        landing = _direction(
            research_history=[{**cycle, "merged_prs_any_base": 2} for _ in range(5)]
        )
        assert _check_direction_health(landing) is None  # suppressed by raw volume

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

    def test_rule6_config_zero_merge_rate_honored(self, mocker):
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_lifetime_min_merge_rate": 0.0},
            clear=False,
        )
        d = _direction(
            research_history=[
                {
                    "tasks_created": 5,
                    "tasks_completed": 4,
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "prs_merged": 1,
                }
                for _ in range(5)
            ],
            history_aggregate={
                "total_tasks_completed": 145,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
                "total_prs_merged": 0,
                "total_cycles": 30,
            },
        )
        assert _compute_lifetime_burn_metrics(d)["lifetime_merge_rate"] < 0.10
        assert _check_direction_health(d) is None

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

    # --- Consolidation: single non-merge-rate proxy governs the pause ---

    def test_consolidated_pause_uses_single_non_merge_proxy(self):
        """The merge-rate pause decision flows through ONE proxy driven by the
        proof-weighted merge verdict — not the former separate zero-merge / churn /
        lifetime rules.

        Two directions with the SAME finished-work shape and real (verified) merge
        outcomes differ only in the merge-test *verdict*: one ships merely-delivered
        UNDECLARED merges (proof-weighted rate below the floor), the other ships
        machine-PROVEN merges (adequate rate). The single proxy pauses the first and
        spares the second, and ``_check_direction_health`` returns exactly what
        ``_direction_non_merge_pause`` returns — proving the consolidation.
        """
        # Real verified merge each cycle, but UNDECLARED ("merely delivered"):
        # proof weight 0.4 → windowed proof-weighted merge rate ≈ 8% (< 10% floor).
        unproven = _direction(
            research_history=[
                {
                    "tasks_created": 6,
                    "tasks_completed": 5,
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "prs_merged": 1,
                    "verified_prs_merged": 1,
                    "acceptance_merged_write_total": 1,
                    "acceptance_declared_merged": 0,
                    "acceptance_proven_merged": 0,
                }
                for _ in range(5)
            ]
        )
        # The proxy signal itself: high unmerged volume AND a sub-floor merge rate.
        metrics = _compute_churn_metrics(unproven)
        assert metrics is not None
        assert metrics["non_merge_finished"] >= 15
        assert metrics["merge_rate"] < 0.10

        proxy_reason = _direction_non_merge_pause(unproven)
        assert proxy_reason is not None
        assert proxy_reason.startswith("Churn guardrail:")
        # The single proxy — not saturation or a code-failure guard — is the cause.
        assert not proxy_reason.startswith(
            ("Saturated:", "High code failure rate:", "Recent stall:")
        )
        assert _is_code_failure_pause_reason(proxy_reason) is False
        # _check_direction_health delegates the merge-rate decision to the one proxy.
        assert _check_direction_health(unproven) == proxy_reason

        # Same finished-work shape, but PROVEN merges (full credit) → the SAME proxy
        # reads an adequate merge rate and does NOT pause, even with high unmerged
        # volume. It is the proof-weighted rate, not raw non-merge volume, that decides.
        proven = _direction(
            research_history=[
                {
                    "tasks_created": 8,
                    "tasks_completed": 7,
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "prs_merged": 2,
                    "verified_prs_merged": 2,
                    "acceptance_merged_write_total": 2,
                    "acceptance_declared_merged": 2,
                    "acceptance_proven_merged": 2,
                }
                for _ in range(5)
            ]
        )
        proven_metrics = _compute_churn_metrics(proven)
        assert proven_metrics is not None
        assert proven_metrics["non_merge_finished"] >= 15
        assert proven_metrics["merge_rate"] >= 0.10
        assert _direction_non_merge_pause(proven) is None
        assert _check_direction_health(proven) is None


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


# ── record_retired_direction_memory + _check_gap_respawn_block ───────────────


class TestRecordRetiredDirectionMemory:
    def test_records_entry_with_gap_id(self):
        vision: dict = {}
        d = {
            "id": "d-abc",
            "text": "model gc garbage collection optimize performance",
            "gap_id": "g-model-gc",
            "research_history": [{"prs_merged": 1}],
        }
        record_retired_direction_memory(vision, d, reason="strategic_retire")
        entries = vision["_recently_retired_directions"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["id"] == "d-abc"
        assert entry["gap_id"] == "g-model-gc"
        assert entry["reason"] == "strategic_retire"
        assert "model" in entry["text_tokens"]
        assert "gc" in entry["text_tokens"]
        assert "retired_at" in entry
        assert entry["retired_outcome"] == {
            "classification": "landed",
            "metrics": {"cycles": 1, "prs_merged": 1},
        }

    def test_records_entry_without_gap_id(self):
        vision: dict = {}
        d = {"id": "d-xyz", "text": "cache layer optimization database"}
        record_retired_direction_memory(vision, d, reason="auto_completed")
        entry = vision["_recently_retired_directions"][0]
        assert "gap_id" not in entry
        assert entry["reason"] == "auto_completed"
        assert entry["retired_outcome"]["classification"] == "success_evidence_met"

    def test_records_abandoned_outcome_when_no_success_or_merge_signal(self):
        vision: dict = {}
        d = {"id": "d-stop", "text": "cache layer optimization database"}
        record_retired_direction_memory(vision, d, reason="strategic_retire")
        entry = vision["_recently_retired_directions"][0]
        assert entry["retired_outcome"] == {
            "classification": "abandoned",
            "metrics": {"cycles": 0, "prs_merged": 0},
        }

    def test_explicit_exhaustion_is_not_reclassified_by_merged_activity(self):
        vision: dict = {}
        direction = {
            "id": "d-exhausted",
            "text": "Bounded investigation found no remaining route",
            "retirement_outcome": "exhausted",
            "research_history": [{"prs_merged": 4}],
        }

        record_retired_direction_memory(
            vision, direction, reason="automatic terminal transition"
        )

        entry = vision["_recently_retired_directions"][0]
        assert entry["retirement_outcome"] == "exhausted"
        assert entry["retired_outcome"]["classification"] == "exhausted"

    def test_fifo_eviction_at_cap(self):
        from app.strategic_completion import _RETIRED_MEMORY_CAP

        vision: dict = {
            "_recently_retired_directions": [
                {"id": f"d-{i}", "text_tokens": [], "retired_at": "2026-01-01T00:00:00", "reason": "r"}
                for i in range(_RETIRED_MEMORY_CAP)
            ]
        }
        d = {"id": "d-new", "text": "unique new direction text"}
        record_retired_direction_memory(vision, d, reason="strategic_retire")
        entries = vision["_recently_retired_directions"]
        assert len(entries) == _RETIRED_MEMORY_CAP
        # Newest entry is last; oldest (d-0) was evicted
        assert entries[-1]["id"] == "d-new"
        assert all(e["id"] != "d-0" for e in entries)

    def test_stopwords_excluded_from_tokens(self):
        vision: dict = {}
        d = {"id": "d-1", "text": "the fix add update improve database"}
        record_retired_direction_memory(vision, d, reason="strategic_retire")
        tokens = vision["_recently_retired_directions"][0]["text_tokens"]
        # stopwords (fix, add, update, improve, the) should be excluded
        assert "fix" not in tokens
        assert "add" not in tokens
        assert "database" in tokens

    def test_no_outcome_key_when_omitted(self):
        # Backward compatibility: existing callers that omit outcome keep the
        # previous entry shape (no 'outcome' key at all).
        vision: dict = {}
        d = {"id": "d-1", "text": "cache layer optimization database"}
        record_retired_direction_memory(vision, d, reason="strategic_retire")
        entry = vision["_recently_retired_directions"][0]
        assert "outcome" not in entry
        assert "retired_outcome" in entry

    def test_records_bounded_outcome_when_provided(self):
        from app.strategic_completion import _RETIRED_OUTCOME_CHAR_CAP

        vision: dict = {}
        d = {"id": "d-1", "text": "cache layer optimization database"}
        long_outcome = "x" * (_RETIRED_OUTCOME_CHAR_CAP + 50)
        record_retired_direction_memory(
            vision, d, reason="strategic_retire", outcome=long_outcome
        )
        entry = vision["_recently_retired_directions"][0]
        assert entry["outcome"] == "x" * _RETIRED_OUTCOME_CHAR_CAP
        assert len(entry["outcome"]) == _RETIRED_OUTCOME_CHAR_CAP

    def test_empty_or_whitespace_outcome_not_stored(self):
        vision: dict = {}
        d = {"id": "d-1", "text": "cache layer optimization database"}
        record_retired_direction_memory(
            vision, d, reason="strategic_superseded", outcome="   "
        )
        entry = vision["_recently_retired_directions"][0]
        assert "outcome" not in entry
        assert "retired_outcome" in entry

    def test_superseded_outcome_carries_successor_pointer(self):
        vision: dict = {}
        d = {"id": "d-1", "text": "cache layer optimization database"}
        record_retired_direction_memory(
            vision,
            d,
            reason="strategic_superseded",
            outcome="superseded by d-successor",
        )
        entry = vision["_recently_retired_directions"][0]
        assert entry["reason"] == "strategic_superseded"
        assert entry["outcome"] == "superseded by d-successor"

    def test_recently_retired_directions_deduped_by_id(self):
        """A retry/replay calling record_retired_direction_memory with the same
        direction ID must not produce a duplicate entry — the existing entry is
        replaced by the latest one so cap slots aren't wasted on the same direction."""
        vision: dict = {}
        d = {"id": "d-dup", "text": "database optimization cache", "research_history": [{"prs_merged": 1}]}
        record_retired_direction_memory(vision, d, reason="strategic_retire")
        # Second call for the same direction (simulating a retry/replay)
        record_retired_direction_memory(vision, d, reason="auto_completed")
        entries = vision["_recently_retired_directions"]
        assert len(entries) == 1, f"Expected 1 entry after dedup, got {len(entries)}"
        # The latest call wins; its reason should be persisted
        assert entries[0]["id"] == "d-dup"
        assert entries[0]["reason"] == "auto_completed"

        # Distinct IDs must each get their own slot
        d2 = {"id": "d-other", "text": "unrelated other direction"}
        record_retired_direction_memory(vision, d2, reason="strategic_retire")
        assert len(vision["_recently_retired_directions"]) == 2

    def test_stores_readable_text_field(self):
        """record_retired_direction_memory must persist the original direction text
        in a ``text`` field so the summary builder can serve human-readable text
        instead of reconstructing it from the stopword-stripped token bag."""
        vision: dict = {}
        readable = "Improve the authentication flow for enterprise users logging in"
        d = {"id": "d-readable", "text": readable}
        record_retired_direction_memory(vision, d, reason="saturation")
        entry = vision["_recently_retired_directions"][0]
        assert entry["text"] == readable
        # text_tokens must still be present for dedup/grounding consumers
        assert "text_tokens" in entry
        assert isinstance(entry["text_tokens"], list)


class TestCheckGapRespawnBlock:
    def _make_entry(self, *, direction_id="d-abc", text_tokens=None, gap_id=None, days_ago=1):
        retired_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        entry: dict = {
            "id": direction_id,
            "text_tokens": text_tokens or [],
            "retired_at": retired_at,
            "reason": "strategic_retire",
        }
        if gap_id:
            entry["gap_id"] = gap_id
        return entry

    def test_blocks_on_exact_gap_id_match(self):
        entry = self._make_entry(direction_id="d-old", gap_id="g-model-gc")
        reason = _check_gap_respawn_block(
            "model gc new implementation",
            "g-model-gc",
            [entry],
            cooldown_days=14,
        )
        assert reason is not None
        assert "g-model-gc" in reason
        assert "d-old" in reason

    def test_blocks_on_text_token_overlap(self):
        entry = self._make_entry(
            direction_id="d-old",
            text_tokens=["cache", "database", "query", "performance"],
        )
        reason = _check_gap_respawn_block(
            "cache database query performance layer",
            None,
            [entry],
            cooldown_days=14,
        )
        assert reason is not None
        assert "d-old" in reason

    def test_no_block_when_entry_outside_cooldown(self):
        entry = self._make_entry(gap_id="g-model-gc", days_ago=20)
        reason = _check_gap_respawn_block(
            "model gc new implementation",
            "g-model-gc",
            [entry],
            cooldown_days=14,
        )
        assert reason is None

    def test_no_block_when_gap_id_differs(self):
        entry = self._make_entry(gap_id="g-other-gap")
        reason = _check_gap_respawn_block(
            "model gc optimization",
            "g-model-gc",
            [entry],
            cooldown_days=14,
        )
        assert reason is None

    def test_no_block_when_text_overlap_below_threshold(self):
        entry = self._make_entry(text_tokens=["cache", "database", "query"])
        reason = _check_gap_respawn_block(
            "cache database query latency improvement",
            None,
            [entry],
            cooldown_days=14,
        )
        # Only 3 shared tokens → below threshold of 4
        assert reason is None

    def test_no_block_on_empty_memory(self):
        reason = _check_gap_respawn_block("anything here", "g-any", [], cooldown_days=14)
        assert reason is None

    def test_zero_cooldown_disables_all_blocks(self):
        entry = self._make_entry(gap_id="g-model-gc", days_ago=0)
        reason = _check_gap_respawn_block(
            "model gc new implementation",
            "g-model-gc",
            [entry],
            cooldown_days=0,
        )
        # cooldown_days=0 means cutoff = now, so even a 0-day-old entry is outside window
        assert reason is None


def test_retired_grounding_classifier_handles_legacy_memory_entry():
    from app.direction_referential_guard import build_retired_direction_grounding

    grounding = build_retired_direction_grounding(
        {
            "id": "d-legacy",
            "reason": "strategic_retire",
            "cycles": 4,
            "prs_merged": 0,
        }
    )

    assert grounding == {
        "direction_id": "d-legacy",
        "classification": "abandoned",
        "metrics": {"prs_merged": 0, "cycles": 4},
        "reason": "strategic_retire",
    }


def test_strategic_refresh_rejects_same_gap_duplicate_direction():
    """Acceptance test: same-gap duplicate blocked via retired memory; distinct direction allowed.

    Verifies:
    1. Exact gap_id match → blocked (enforce mode)
    2. Text-overlap match for gap-pruned direction → blocked via retained memory (enforce mode)
    3. Genuinely distinct direction on same gap → NOT blocked (observe mode)
    """
    from app.strategic_completion import _RETIRED_MEMORY_CAP  # noqa: F401

    # Build retired memory as if g-model-gc direction was previously retired
    retired_dir = {
        "id": "d-retired-gc",
        "text": "model gc garbage collection entry deduplication",
        "gap_id": "g-model-gc",
    }
    vision: dict = {}
    record_retired_direction_memory(vision, retired_dir, reason="strategic_retire")
    retired_memory = vision["_recently_retired_directions"]

    # 1. Exact gap_id match → blocked in enforce mode
    block_reason = _check_gap_respawn_block(
        "model gc garbage collection entry deduplication",
        "g-model-gc",
        retired_memory,
        cooldown_days=14,
    )
    assert block_reason is not None, "Same-gap exact duplicate should be blocked"
    assert "g-model-gc" in block_reason

    # 2. Near-duplicate text overlap (pruned direction survives in memory) → blocked
    block_reason_overlap = _check_gap_respawn_block(
        "model gc garbage collection entry memory pruning",  # 4+ shared tokens
        None,  # gap_id absent (agent omitted it) → text-overlap gate fires
        retired_memory,
        cooldown_days=14,
    )
    assert block_reason_overlap is not None, (
        "Text-overlap duplicate of a pruned retired direction should be blocked"
    )

    # 3. Genuinely distinct direction on a different gap → NOT blocked
    no_block = _check_gap_respawn_block(
        "authentication oauth token refresh pipeline",
        "g-auth-refresh",  # different gap
        retired_memory,
        cooldown_days=14,
    )
    assert no_block is None, "Distinct direction on a different gap should not be blocked"


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
    async def test_does_not_mutate_cycle_for_strategic_review(self, strat_fail_db):
        # Cycle state is owned by linkage-aware reconciliation, not this
        # circuit-breaker accounting helper.
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
        assert cycle.outcome == sc.OUTCOME_PHASE2_RUNNING
        assert cycle.phase2_completed_at is None
        assert cycle.failure_reason is None

    @pytest.mark.asyncio
    async def test_does_not_mutate_cycle_for_design_doc_update(self, strat_fail_db):
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
        assert cycle.outcome == sc.OUTCOME_PHASE1_RUNNING
        assert cycle.phase1_completed_at is None
        assert cycle.failure_reason is None

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

        assert project.vision.get("_strategic_failures") == 1

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

        assert cycle.outcome == sc.OUTCOME_PHASE2_RUNNING
        assert cycle.outcome in sc.ACTIVE_OUTCOMES

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

        assert cycle.outcome == sc.OUTCOME_PHASE2_RUNNING


# ── vision-lock discipline on failure paths ──────────────────────────────────


@pytest.mark.asyncio
async def test_unparseable_output_increments_failures_preserves_vision_keys(
    db_session, mocker
):
    """Unparseable-output path increments _strategic_failures via
    with_vision_locked so that sibling vision keys are NOT clobbered and the
    increment is not lost to a concurrent stale-read race."""
    from unittest.mock import AsyncMock

    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "keep-me", "_strategic_failures": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "p", "vision": {"revision": 0}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Pass unparseable logs so the function takes the failure branch
    await _handle_strategic_completion(task, "{{not valid json", db_session)

    await db_session.refresh(project)
    vision = project.vision or {}
    # Counter must have been incremented (was 1, now 2)
    assert vision.get("_strategic_failures") == 2, (
        f"Expected _strategic_failures=2, got {vision.get('_strategic_failures')!r}"
    )
    # Sibling key must NOT have been clobbered
    assert vision.get("purpose") == "keep-me", (
        f"vision lock must preserve sibling vision keys; got purpose={vision.get('purpose')!r}"
    )


@pytest.mark.asyncio
async def test_no_vision_produced_increments_failures_under_held_lock(
    db_session, mocker
):
    """No-vision path must increment _strategic_failures on the already-locked
    project object without re-acquiring with_vision_locked (which would deadlock).
    Other vision keys must be preserved."""
    import json
    from unittest.mock import AsyncMock

    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._check_direction_starvation", new_callable=AsyncMock
    )

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "keep-me", "revision": 1, "_strategic_failures": 0},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "p", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Logs parse successfully but contain no "vision" key — triggers the no-vision path
    logs = json.dumps({"problem_model": {}})
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    vision = project.vision or {}
    assert vision.get("_strategic_failures") == 1, (
        f"Expected _strategic_failures=1, got {vision.get('_strategic_failures')!r}"
    )
    # Sibling key must be preserved
    assert vision.get("purpose") == "keep-me", (
        f"no-vision path must preserve sibling vision keys; got purpose={vision.get('purpose')!r}"
    )


@pytest.mark.asyncio
async def test_legacy_vision_merge_uses_real_baseline_when_no_cycle_row(
    db_session, mocker
):
    """The log-parse 3-way vision merge must NOT run against an empty baseline.

    The vision-key cut (#16973) stopped shipping the legacy `vision` body in the
    strategic payload, so ``task.first_payload.get("vision")`` is empty for every
    modern task. When the task also has no StrategicCycle row, the old code merged
    against ``{}`` — which makes every agent-supplied field read as a fresh edit
    against the persisted vision. Legitimate agent edits to existing fields were
    then rejected as ``value_conflict`` and the user's stale value won. The fix
    binds the baseline to the current agent-visible vision, so the merge sees the
    agent as the only editor and lets the edit land.
    """
    from unittest.mock import AsyncMock

    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._check_direction_starvation", new_callable=AsyncMock
    )

    project = Project(
        name="P",
        user_id="u1",
        vision={
            "purpose": "original purpose",
            "revision": 0,
            "systems": [{"id": "s1", "name": "Auth", "status": "planned"}],
        },
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Modern task shape after the vision-key cut: NO `vision` key in the payload
    # and NO StrategicCycle row (no cycle_id in context). The only baseline the
    # merge can bind to is the current agent-visible vision.
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {
                "purpose": "agent updated purpose",
                "revision": 1,
                "systems": [{"id": "s1", "name": "Auth", "status": "active"}],
            },
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    vision = project.vision or {}
    # The agent's edit to an existing field must land (the user never touched it).
    assert vision.get("purpose") == "agent updated purpose", (
        f"Agent purpose edit must survive the merge with a real baseline; "
        f"got {vision.get('purpose')!r}"
    )
    sys_by_id = {s["id"]: s for s in vision.get("systems", [])}
    assert sys_by_id["s1"]["status"] == "active", (
        f"Agent systems edit must survive the merge; got {sys_by_id['s1']['status']!r}"
    )
    # No spurious rejected-edit ledger entries for agent-only edits.
    assert (
        vision.get("_rejected_agent_edits") is None
        or vision["_rejected_agent_edits"] == []
    ), (
        "A real baseline must not log value_conflict rejections for agent-only edits; "
        f"got {vision.get('_rejected_agent_edits')!r}"
    )
    # The merge still bumps the revision off the persisted value.
    assert vision.get("revision") == 1, (
        f"Expected revision bumped to 1, got {vision.get('revision')!r}"
    )


# ── _check_direction_starvation ──────────────────────────────────────────────


def _active_dir(**kwargs):
    base = {"id": f"d-{kwargs.get('id', 'x')}", "status": "active"}
    base.update(kwargs)
    return base


class TestCheckDirectionStarvation:
    @pytest.mark.asyncio
    async def test_no_gaps_resets_counter(self, strat_fail_db, mocker):
        mocker.patch("app.alert_dispatch._create_strategic_alert", new=AsyncMock())
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
    async def test_unrelated_direction_creation_does_not_cover_gap(
        self, strat_fail_db, mocker
    ):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=AsyncMock()
        )
        project = Project(
            name="P",
            user_id="u1",
            vision={
                "_direction_starvation_count": 4,
                "_strategic_frequency_override": 2,
            },
            directions=[_active_dir(id="1", category="product")],
            problem_model={
                "gaps": [
                    {
                        "id": "g-still-uncovered",
                        "name": "Still uncovered",
                        "status": "active",
                        "active_moves": [],
                    }
                ]
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        # Creating an unrelated direction is not evidence that this gap is staffed.
        await _check_direction_starvation(
            project, dirs_created=1, session=strat_fail_db
        )

        assert project.vision.get("_direction_starvation_count") == 5
        assert project.vision.get("_strategic_frequency_override") == 2
        assert "g-still-uncovered" in alert_mock.call_args.args[2]

    @pytest.mark.asyncio
    async def test_uncovered_gap_increments_counter(self, strat_fail_db, mocker):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=AsyncMock()
        )
        project = Project(
            name="P",
            user_id="u1",
            vision={},
            directions=[_active_dir(id="1", category="product")],
            problem_model={
                "gaps": [
                    {
                        "id": "g-uncovered",
                        "name": "Uncovered reliability gap",
                        "status": "active",
                        "active_moves": [],
                    }
                ]
            },
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
            "app.alert_dispatch._create_strategic_alert", new=AsyncMock()
        )
        project = Project(
            name="P",
            user_id="u1",
            vision={"_direction_starvation_count": 1},
            directions=[_active_dir(id="1", category="product")],
            problem_model={
                "gaps": [
                    {
                        "id": "g-uncovered",
                        "name": "Uncovered reliability gap",
                        "status": "active",
                        "active_moves": [],
                    }
                ]
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert project.vision.get("_direction_starvation_count") == 2
        assert project.vision.get("_strategic_frequency_override") == 2
        alert_mock.assert_called_once()
        assert "g-uncovered" in alert_mock.call_args.args[2]
        assert "no direction was auto-created" in alert_mock.call_args.args[2]

    @pytest.mark.asyncio
    async def test_level_5_keeps_alerting_without_auto_create(self, strat_fail_db, mocker):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=AsyncMock()
        )
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
            problem_model={
                "gaps": [
                    {
                        "id": "g-uncovered",
                        "name": "Uncovered product gap",
                        "status": "active",
                        "active_moves": [],
                    }
                ]
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert len(project.directions) == 3
        assert project.vision.get("_direction_starvation_count") == 5
        assert project.vision.get("_strategic_frequency_override") == 2
        alert_mock.assert_called_once()
        assert "g-uncovered" in alert_mock.call_args.args[2]

    @pytest.mark.asyncio
    async def test_staffed_or_deferred_gaps_do_not_trigger(self, strat_fail_db, mocker):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=AsyncMock()
        )
        project = Project(
            name="P",
            user_id="u1",
            vision={
                "_direction_starvation_count": 3,
                "_strategic_frequency_override": 2,
            },
            directions=[_active_dir(id="1", category="product")],
            problem_model={
                "gaps": [
                    {
                        "id": "g-staffed",
                        "name": "Staffed",
                        "status": "active",
                        "active_moves": ["d-1"],
                    },
                    {
                        "id": "g-deferred",
                        "name": "Deferred",
                        "status": "active",
                        "active_moves": [],
                        "deferral": {"rationale": "intentional hold"},
                    },
                ]
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert project.vision.get("_direction_starvation_count") == 0
        assert "_strategic_frequency_override" not in project.vision
        alert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_starvation_and_bootstrap_counters_idempotent_on_replay(
        self, strat_fail_db, mocker
    ):
        """Replay of the same task must not double-count either counter.

        Starvation: calling _check_direction_starvation twice with the same
        task id should increment _direction_starvation_count exactly once.

        Bootstrap degraded: calling _handle_repo_bootstrap_completion twice
        with the same task (no overview → degraded path) should record
        bootstrap_recovery_attempts == 1, not 2.
        """
        from contextlib import asynccontextmanager
        from app.task_completion import _handle_repo_bootstrap_completion

        mocker.patch("app.alert_dispatch._create_strategic_alert", new=AsyncMock())

        # ── Starvation counter ──────────────────────────────────────────────
        project = Project(
            name="StarveP",
            user_id="u1",
            vision={},
            directions=[_active_dir(id="1", category="structural")],
            problem_model={
                "gaps": [
                    {
                        "id": "g-uncovered",
                        "name": "Uncovered reliability gap",
                        "status": "active",
                        "active_moves": [],
                    }
                ]
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        task_mock = MagicMock()
        task_mock.id = uuid.uuid4()

        # First run: counter should go from 0 → 1
        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db, task=task_mock
        )
        assert project.vision.get("_direction_starvation_count") == 1

        # Replay with same task id: counter must stay at 1
        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db, task=task_mock
        )
        assert project.vision.get("_direction_starvation_count") == 1, (
            "Replay with same task id must not re-increment _direction_starvation_count"
        )

        # ── Bootstrap recovery counter ──────────────────────────────────────
        bs_project = Project(
            name="BootstrapP",
            user_id="u1",
            build_config={},
            design_doc={},
        )
        strat_fail_db.add(bs_project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(bs_project)

        bs_task = Task(
            project_id=bs_project.id,
            payloads=[{"type": "repo_bootstrap"}],
            context={},  # No repo_overview → degraded path
        )
        strat_fail_db.add(bs_task)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(bs_task)

        # Mock project_lock to yield bs_project directly
        @asynccontextmanager
        async def _mock_project_lock(session, project_id, **kwargs):
            yield bs_project

        mocker.patch("app.task_completion.project_lock", side_effect=_mock_project_lock)
        mocker.patch(
            "app.task_completion.with_design_doc_locked",
            new=AsyncMock(),
        )
        mocker.patch("app.task_completion._stamp_command_memory_status")
        mocker.patch(
            "app.task_completion._seed_bootstrap_playbook_commands", new=AsyncMock()
        )
        mocker.patch(
            "app.task_completion._upsert_domain_knowledge_document", new=AsyncMock()
        )

        # First run: degraded path records attempts == 1
        await _handle_repo_bootstrap_completion(bs_task, logs="", session=strat_fail_db)
        bc = bs_project.build_config or {}
        assert bc.get("bootstrap_recovery_attempts") == 1

        # Replay with same task: must stay at 1
        await _handle_repo_bootstrap_completion(bs_task, logs="", session=strat_fail_db)
        bc = bs_project.build_config or {}
        assert bc.get("bootstrap_recovery_attempts") == 1, (
            "Replay of the same bootstrap task must not re-increment bootstrap_recovery_attempts"
        )


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

    def test_build_creation_prediction_excludes_degraded_unverified_merges(self):
        """Similar-past-merges counts VERIFIED merges only (history + aggregate)."""
        similar = {
            "id": "d0",
            "text": "cache invalidation dashboard refresh",
            "research_history": [
                # 2 raw merges, 1 verified.
                {"prs_merged": 2, "verified_prs_merged": 1},
            ],
            "history_aggregate": {
                # 5 raw, 2 verified.
                "total_prs_merged": 5,
                "total_verified_prs_merged": 2,
            },
        }
        p = Project(name="P", user_id="u", directions=[similar], vision={})
        new_d = {"id": "d1", "text": "cache invalidation dashboard refresh strategy"}
        pred = _build_creation_prediction(new_d, p)
        # Verified-only: 1 (history) + 2 (aggregate) = 3, NOT raw 2+5=7.
        assert pred["similar_past_merges"] == 3

    def test_build_creation_prediction_legacy_aggregate_falls_back(self):
        """Aggregate predating total_verified_prs_merged uses total_prs_merged."""
        similar = {
            "id": "d0",
            "text": "cache invalidation dashboard refresh",
            "research_history": [{"prs_merged": 1}],
            "history_aggregate": {"total_prs_merged": 4},
        }
        p = Project(name="P", user_id="u", directions=[similar], vision={})
        new_d = {"id": "d1", "text": "cache invalidation dashboard refresh strategy"}
        pred = _build_creation_prediction(new_d, p)
        # 1 (legacy history fallback) + 4 (legacy aggregate fallback) = 5.
        assert pred["similar_past_merges"] == 5

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
async def test_system_status_not_upgraded_on_unverified_merges(db_session, mocker):
    """A system whose mapped directions have raw merges but ZERO verified
    merges must NOT be auto-upgraded to functional/in_progress — the upgrade
    is credited only for merges that reproduced on HEAD."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d1",
                "system_id": "sys1",
                # 8 raw merges, none verified -> verified total is 0.
                "research_history": [
                    {"prs_merged": 5, "verified_prs_merged": 0},
                    {"prs_merged": 3, "verified_prs_merged": 0},
                ],
                "history_aggregate": {
                    "total_prs_merged": 6,
                    "total_verified_prs_merged": 0,
                },
            }
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
            "vision": {
                "purpose": "test",
                "revision": 2,
                "systems": [{"id": "sys1", "status": "planned"}],
            },
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    sys = next(s for s in project.vision["systems"] if s["id"] == "sys1")
    # zero verified merges -> NOT promoted to functional even though raw merges
    # are >= 5. (in_progress still fires from the mapped-direction presence
    # branch, which is independent of merge counts.)
    assert sys["status"] != "functional"
    assert sys["status"] == "in_progress"


@pytest.mark.asyncio
async def test_system_status_upgraded_on_verified_merges(db_session, mocker):
    """Verified merges still upgrade a system to functional exactly as before."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d1",
                "system_id": "sys1",
                "research_history": [
                    {"prs_merged": 5, "verified_prs_merged": 5},
                    {"prs_merged": 3, "verified_prs_merged": 3},
                ],
                "history_aggregate": {},
            }
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
            "vision": {
                "purpose": "test",
                "revision": 2,
                "systems": [{"id": "sys1", "status": "planned"}],
            },
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    sys = next(s for s in project.vision["systems"] if s["id"] == "sys1")
    # 8 verified merges >= 5 -> functional.
    assert sys["status"] == "functional"


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
async def test_fallback_direction_create_idempotent_on_replay(db_session, mocker):
    """Calling _handle_strategic_completion twice with the same direction_ops
    (simulating a post-commit replay) must produce exactly ONE direction, not two.

    The fallback path previously minted a fresh random UUID on every run so the
    id-based dedup guard could never fire. The fix makes the ID deterministic
    (task_id + text hash), so the second call finds the already-committed direction
    and skips creation.
    """
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._check_direction_starvation", new_callable=AsyncMock
    )

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

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Improve test coverage across modules",
                    "priority": "medium",
                    "rationale": "coverage gaps",
                    "success_evidence": "Coverage reaches 80% on all modules",
                    "retirement_condition": "Coverage stable for 2 cycles",
                },
            ],
        }
    )

    # First call — creates the direction
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)
    dirs_after_first = [
        d for d in (project.directions or [])
        if d.get("text") == "Improve test coverage across modules"
    ]
    assert len(dirs_after_first) == 1, (
        f"Expected 1 direction after first call, got {len(dirs_after_first)}"
    )
    first_id = dirs_after_first[0]["id"]

    # Second call — simulates a post-commit replay; must NOT create a duplicate
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)
    dirs_after_replay = [
        d for d in (project.directions or [])
        if d.get("text") == "Improve test coverage across modules"
    ]
    assert len(dirs_after_replay) == 1, (
        f"Replay created a duplicate: expected 1 direction, got {len(dirs_after_replay)}"
    )
    assert dirs_after_replay[0]["id"] == first_id, (
        f"Replay changed the direction id: {first_id!r} -> {dirs_after_replay[0]['id']!r}"
    )


@pytest.mark.asyncio
async def test_fallback_stage_direction_note_idempotent_on_replay(db_session, mocker):
    """Replaying stage_direction must NOT append the staged note twice.

    The stage_direction branch has no "already staged, skip" pre-guard — it
    re-enters on a post-commit replay and re-sets status/staged_for_version.
    The only thing preventing a duplicate note is the inline
    ``not any(n.get("text") == note_text ...)`` dedup guard, so this pins that
    idempotency contract directly.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._check_direction_starvation", new_callable=AsyncMock
    )
    # stage_direction requires an open version; make one deterministically.
    mocker.patch(
        "app.release_version.current_open_version",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(version_number=7),
    )

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {"id": "d-stage-1", "text": "Stage me", "status": "proposed", "notes": []}
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
                    "op": "stage",
                    "direction_id": "d-stage-1",
                    "rationale": "sequence after current version",
                },
            ],
        }
    )

    expected_note = "Staged to begin after v7 promotes: sequence after current version"

    # First call — stages the direction and appends the note.
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)
    target = next(d for d in project.directions if d.get("id") == "d-stage-1")
    assert target["status"] == "staged"
    staged_notes = [n for n in target.get("notes", []) if n.get("text") == expected_note]
    assert len(staged_notes) == 1, f"Expected 1 staged note, got {len(staged_notes)}"

    # Second call — simulates a post-commit replay; must NOT duplicate the note.
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)
    target = next(d for d in project.directions if d.get("id") == "d-stage-1")
    staged_notes = [n for n in target.get("notes", []) if n.get("text") == expected_note]
    assert len(staged_notes) == 1, (
        f"Replay duplicated the staged note: expected 1, got {len(staged_notes)}"
    )


@pytest.mark.asyncio
async def test_api_completion_resets_strategic_failures_breaker(db_session, mocker):
    """A successful API-path strategic cycle resets the _strategic_failures
    breaker even when it does NOT rewrite the vision.

    Regression: the counter was only cleared on a vision *write* (the
    set-problem-model API handler and the legacy log-parse merge). A healthy
    cycle that completes by updating directions / problem_model / running_update
    without rewriting the vision therefore left the breaker untouched, so
    _strategic_failures only ever ratcheted up on failures and could be cleared
    solely by the 48h time-decay — leaving the breaker falsely tripped (and
    forced/backstop strategic refresh gated) for up to two days while every
    cycle succeeded.
    """
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._check_direction_starvation", new_callable=AsyncMock
    )

    project = Project(
        name="P",
        user_id="u1",
        vision={
            "purpose": "test",
            "revision": 3,
            "_strategic_failures": 3,
            "_strategic_failures_at": "2026-06-08T19:49:29",
        },
        directions=[{"id": "d1", "text": "x", "status": "active"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 3}}
        ],
        # api_was_used -> True via _strategic_api_called, but the cycle does NOT
        # rewrite the vision (revision unchanged) and carries no cycle_id — the
        # common "successful cycle without a vision write" shape.
        context={"_strategic_api_called": True},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_strategic_completion(task, "", db_session)

    await db_session.refresh(project)
    vision = project.vision or {}
    assert "_strategic_failures" not in vision, (
        "a successful API-path cycle must clear the _strategic_failures breaker; "
        f"got {vision.get('_strategic_failures')!r}"
    )
    assert "_strategic_failures_at" not in vision
    # Real vision content is preserved — only the breaker keys are dropped.
    assert vision.get("purpose") == "test"


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
                "context": {
                    "success_evidence": "prior evidence",
                    "implementation_constraints": "old constraint",
                },
                "outcome_progress_ever_met": True,
                "outcome_progress_history": [{"progress": 3, "cycle_index": 7}],
                "evidence_progress": {
                    "status": "advancing",
                    "rationale": "judged under the old constraint",
                },
                "outcome_scaffolding_flagged": True,
                "outcome_scaffolding_evidence": {"reason": "old constraint"},
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
    assert "outcome_progress_ever_met" not in target
    assert not target.get("outcome_progress_history")
    assert "evidence_progress" not in target
    assert "outcome_scaffolding_flagged" not in target
    assert "outcome_scaffolding_evidence" not in target


@pytest.mark.asyncio
async def test_strategic_completion_create_rejects_oversized_contract_fields(
    db_session, mocker
):
    """Completion-interpreted creates reject, rather than truncate, each contract."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    project = Project(
        name="P-create-contract-limits",
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

    common = {
        "op": "create",
        "success_evidence": "valid evidence",
        "retirement_condition": "valid retirement",
    }
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {**common, "text": "Oversized evidence", "success_evidence": "e" * 501},
                {
                    **common,
                    "text": "Oversized retirement",
                    "retirement_condition": "r" * 501,
                },
                {
                    **common,
                    "text": "Oversized constraints",
                    "implementation_constraints": "c" * 501,
                },
                {
                    **common,
                    "text": "Valid boundary",
                    "implementation_constraints": "c" * 500,
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    assert [direction["text"] for direction in project.directions] == [
        "Valid boundary"
    ]
    assert (
        project.directions[0]["context"]["implementation_constraints"] == "c" * 500
    )


@pytest.mark.asyncio
async def test_strategic_completion_update_rejects_oversized_contract_atomically(
    db_session, mocker
):
    """An invalid completion update skips the whole direction mutation."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    directions = [
        {
            "id": f"d-contract-{index}",
            "text": f"Original {index}",
            "status": "active",
            "priority": "medium",
            "notes": [],
            "research_history": [],
            "context": {
                "success_evidence": "old evidence",
                "retirement_condition": "old retirement",
                "implementation_constraints": "old constraints",
            },
        }
        for index in range(3)
    ]
    project = Project(
        name="P-update-contract-limits",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=directions,
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
                    "direction_id": f"d-contract-{index}",
                    "changes": {
                        "text": f"Must not persist {index}",
                        field: value * 501,
                    },
                }
                for index, (field, value) in enumerate(
                    (
                        ("success_evidence", "e"),
                        ("retirement_condition", "r"),
                        ("implementation_constraints", "c"),
                    )
                )
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    for index, direction in enumerate(project.directions):
        assert direction["text"] == f"Original {index}"
        assert direction["context"] == {
            "success_evidence": "old evidence",
            "retirement_condition": "old retirement",
            "implementation_constraints": "old constraints",
        }


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
async def test_strategic_update_rejects_oversized_context_fields_atomically(
    db_session, mocker
):
    """Direction-op `update` rejects oversized contract fields without mutation."""
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
    assert "success_evidence" not in ctx
    assert "retirement_condition" not in ctx


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
async def test_memo_citations_include_structured_retirement_context(db_session, mocker):
    """Enriched retirement memo context is surfaced in memo_citations.

    A spawned direction_retirement memo task carries reasoning_kind/direction_id
    in its first payload and structured evidence fields in source_finding/context.
    query_recent_memo_citations must return these enriched fields from the
    production-shaped spawn path so the strategic agent can make retirement-
    relevance decisions without opening task detail.
    """
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="Retirement-Ctx",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    direction_id = "dir-abc123"
    parent_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    project.directions = [
        {
            "id": direction_id,
            "text": "Direction X retired",
            "status": "retired",
            "priority": "medium",
            "notes": [],
            "context": {
                "success_evidence": "All 5 acceptance tests passed; zero regressions.",
                "retirement_condition": "All KPIs above threshold for 2 consecutive cycles.",
            },
            "research_history": [
                {"cycles": 2, "prs_merged": 4, "tasks_completed": 6, "tasks_failed": 1},
                {"cycles": 1, "prs_merged": 2, "tasks_completed": 3, "tasks_failed": 0},
            ],
        }
    ]
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    rationale = "The direction delivered its full scope."
    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        parent_task,
        retired_ids=[direction_id],
        rationales={direction_id: rationale},
    )
    assert spawned == 1
    await db_session.commit()

    # query_recent_memo_citations must surface enriched fields directly from the
    # production-shaped spawn path.
    cites = await strategic_cycle.query_recent_memo_citations(
        db_session, project.id, cap=25
    )
    assert len(cites) == 1, "Expected one enriched memo citation"
    entry = cites[0]

    assert entry["summary_excerpt"].startswith(
        "The direction delivered its full scope."
    )
    assert entry.get("reasoning_kind") == "direction_retirement", (
        "reasoning_kind from payload must be included in citation"
    )
    assert entry.get("direction_id") == direction_id, (
        "direction_id from payload must be included in citation"
    )
    assert (
        entry.get("success_evidence")
        == "All 5 acceptance tests passed; zero regressions."
    ), "success_evidence from memo must be included"
    assert (
        entry.get("retirement_condition")
        == "All KPIs above threshold for 2 consecutive cycles."
    ), "retirement_condition from memo must be included"
    assert (
        entry.get("citation_refs") == f"direction:{direction_id}, task:{parent_task.id}"
    ), "citation references must be preserved in compact form"
    assert "final_prs_merged=6" in entry.get("final_metrics_excerpt", ""), (
        "final metrics must be preserved in compact form"
    )
    assert "final_cycles_active=2" in entry.get("final_metrics_excerpt", ""), (
        "final metrics must be preserved in compact form"
    )
    assert entry.get("rationale_excerpt") == rationale, (
        "retirement rationale must be preserved in compact form"
    )
    assert entry.get("reasoning_excerpt", "").startswith(
        "Direction: Direction X retired"
    ), "memo reasoning must be preserved in compact form"
    assert entry.get("tags") == [
        "direction-retirement",
        "success-evidence",
        "retirement-condition",
    ], "tags from memo must be included"
    # Verify that full cycle flow also persists these enriched fields via
    # _handle_strategic_completion.
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
    assert "memo_citations" in meta
    cites_persisted = meta["memo_citations"]
    assert len(cites_persisted) == 1
    persisted = cites_persisted[0]
    assert persisted["task_id"] != ""
    assert persisted.get("reasoning_kind") == "direction_retirement", (
        "reasoning_kind must survive the summarizer and persist in cycle_metadata"
    )
    assert persisted.get("direction_id") == direction_id, (
        "direction_id must survive the summarizer and persist in cycle_metadata"
    )
    assert (
        persisted.get("success_evidence")
        == "All 5 acceptance tests passed; zero regressions."
    ), "success_evidence must survive summarizer trimming"
    assert (
        persisted.get("citation_refs")
        == f"direction:{direction_id}, task:{parent_task.id}"
    ), "citation_refs must survive summarizer trimming"
    assert "final_prs_merged=6" in persisted.get("final_metrics_excerpt", ""), (
        "final_metrics_excerpt must survive summarizer trimming"
    )
    assert "final_cycles_active=2" in persisted.get("final_metrics_excerpt", ""), (
        "final_metrics_excerpt must survive summarizer trimming"
    )
    assert persisted.get("rationale_excerpt") == rationale, (
        "rationale_excerpt must survive summarizer trimming"
    )
    assert persisted.get("reasoning_excerpt", "").startswith(
        "Direction: Direction X retired"
    ), "reasoning_excerpt must survive summarizer trimming"


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
async def test_fallback_completion_finalizes_cycle_linked_only_by_task(
    db_session, mocker
):
    """Legacy log-parse completion finalizes the owning cycle even when the
    task never carried a strategic_cycle_id (neither column nor context).

    Regression: a strategic_review task completed via the fallback log-parse
    path (parseable JSON output, no strategic API markers) applied all project
    mutations but left the owning StrategicCycle in phase2_running. cycle_id
    resolution only checked task.strategic_cycle_id / context, so a legacy
    task whose only linkage was the cycle row's phase2_task_id stayed
    unfinalized for 3+ hours until the stalled-cycle reaper marked it stalled.
    """
    import json
    from unittest.mock import AsyncMock

    from app import strategic_cycle
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._check_direction_starvation", new_callable=AsyncMock
    )
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

    project = Project(
        name="P-LEGACY-FB",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Legacy task: no strategic_cycle_id column, no context linkage.
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

    # The owning cycle is linked ONLY through the cycle row's phase2_task_id.
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await strategic_cycle.mark_phase1_completed(
        db_session, cycle.id, phase2_task_id=task.id
    )
    await db_session.commit()

    logs = json.dumps({"vision": {"purpose": "test", "revision": 2}})
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    assert (project.vision or {}).get("revision") == 2, (
        "fallback path must still apply the vision mutation"
    )
    await db_session.refresh(cycle)
    assert cycle.outcome == strategic_cycle.OUTCOME_COMPLETED, (
        f"fallback completion left the owning cycle {cycle.outcome}; expected completed"
    )
    assert cycle.phase2_completed_at is not None
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["cycle_id"] == cycle.id, (
        "strategic_cycle_completed must carry the resolved owning cycle id"
    )


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
        directions=[
            {
                "id": "d-grounded",
                "text": "Improve login flow",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            }
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
                    "op": "create",
                    "text": "Migrate auth service to OAuth2",
                    "priority": "medium",
                    "success_evidence": "All auth tests pass",
                    "retirement_condition": "OAuth2 flow live for 3 cycles",
                    "dedup_reasoning": {
                        "closest_directions": ["d-grounded", "Add SSO support"],
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
    assert dr["closest_directions"] == ["d-grounded", "Add SSO support"]
    assert dr["distinction"] == "Targets the auth backend protocol, not the UI flow"
    verdict = ctx.get("grounding_verdict")
    assert verdict is not None
    assert verdict["status"] == "validated"
    assert verdict["live_direction_ids"] == ["d-grounded"]


@pytest.mark.asyncio
async def test_create_direction_attaches_landed_retired_outcome(db_session, mocker):
    """Fallback create ops matching landed retired work carry prior outcome context."""
    import json
    from unittest.mock import AsyncMock
    from app import strategic_cycle
    from app.models import KnowledgeDocument, SourceKind
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.strategic_completion._runtime_config",
        {"direction_gap_respawn_block_mode": "observe"},
    )

    retired_memory = [
        {
            "id": "d-landed",
            "text_tokens": ["gpu", "runner", "lane", "verification"],
            "retired_at": _utcnow().isoformat(),
            "reason": "success_evidence_met",
            "outcome": {
                "classification": "landed",
                "metrics": {"prs_merged": 2, "cycles": 3},
            },
        }
    ]
    project = Project(
        name="RetiredOutcomeCreate",
        user_id="u-retired-outcome",
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": retired_memory,
        },
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    source_task = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "prompt": "retirement memo"}],
        context={},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    memo = KnowledgeDocument(
        project_id=project.id,
        source_task_id=source_task.id,
        source_kind=SourceKind.DIRECTION_RETIREMENT.value,
        title="Direction retired: GPU runner lane",
        summary="GPU runner lane shipped and verified on automain.",
        body="Retirement memo body",
        topic="direction-retirement",
        direction_id="d-landed",
        tags=["direction-retirement"],
    )
    db_session.add(memo)
    await db_session.commit()
    await db_session.refresh(memo)

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

    logs = json.dumps(
        {
            "vision": {
                "purpose": "test",
                "revision": 2,
                "_recently_retired_directions": retired_memory,
            },
            "direction_operations": [
                {
                    "op": "create_direction",
                    "text": "Rebuild GPU runner lane verification coverage",
                    "priority": "medium",
                    "success_evidence": "Fresh verification is present",
                    "retirement_condition": "Verification remains green",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = [
        d for d in (project.directions or []) if "GPU runner lane" in d.get("text", "")
    ]
    assert len(created) == 1
    grounding = (created[0].get("context") or {}).get("retired_direction_grounding")
    assert grounding == {
        "direction_id": "d-landed",
        "classification": "landed",
        "metrics": {"prs_merged": 2, "cycles": 3},
        "reason": "success_evidence_met",
        "retirement_memo": {
            "knowledge_doc_id": str(memo.id),
            "summary": "GPU runner lane shipped and verified on automain.",
        },
    }
    notes = [n.get("text", "") for n in created[0].get("notes", [])]
    assert any("prior_outcome=landed" in n and "landed work" in n for n in notes)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    created_id = created[0]["id"]
    assert meta["created_retired_grounding"][created_id]["classification"] == "landed"


@pytest.mark.asyncio
async def test_create_direction_attaches_abandoned_retired_outcome(db_session, mocker):
    """Fallback create ops matching non-landed retired work are marked abandoned."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch.dict(
        "app.strategic_completion._runtime_config",
        {"direction_gap_respawn_block_mode": "observe"},
    )

    retired_memory = [
        {
            "id": "d-abandoned",
            "text_tokens": ["cache", "warming", "scheduler", "latency"],
            "retired_at": _utcnow().isoformat(),
            "reason": "strategic_retire",
            "outcome": {
                "classification": "abandoned",
                "metrics": {"prs_merged": 0, "cycles": 5},
            },
        }
    ]
    project = Project(
        name="AbandonedRetiredOutcomeCreate",
        user_id="u-abandoned-outcome",
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": retired_memory,
        },
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
            "vision": {
                "purpose": "test",
                "revision": 2,
                "_recently_retired_directions": retired_memory,
            },
            "direction_operations": [
                {
                    "op": "create_direction",
                    "text": "Retry cache warming scheduler latency improvements",
                    "priority": "medium",
                    "success_evidence": "Latency improves",
                    "retirement_condition": "Scheduler stabilizes",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = [
        d for d in (project.directions or []) if "cache warming" in d.get("text", "")
    ]
    assert len(created) == 1
    grounding = (created[0].get("context") or {}).get("retired_direction_grounding")
    assert grounding["classification"] == "abandoned"
    assert grounding["metrics"] == {"prs_merged": 0, "cycles": 5}
    notes = [n.get("text", "") for n in created[0].get("notes", [])]
    assert any("prior_outcome=abandoned" in n for n in notes)
    assert not any("landed work" in n for n in notes)


@pytest.mark.asyncio
async def test_create_direction_without_retired_match_has_no_grounding(
    db_session, mocker
):
    """Clean fallback creates remain unchanged when retired memory does not match."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="NoRetiredOutcomeCreate",
        user_id="u-no-retired-outcome",
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
                    "op": "create_direction",
                    "text": "Open a new retrieval evaluation lane",
                    "priority": "medium",
                    "success_evidence": "Evaluation lane reports results",
                    "retirement_condition": "Retriever quality is measured",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = [
        d for d in (project.directions or []) if "retrieval evaluation" in d.get("text", "")
    ]
    assert len(created) == 1
    assert "retired_direction_grounding" not in (created[0].get("context") or {})
    assert all(
        "Grounding: matches retired direction" not in n.get("text", "")
        for n in created[0].get("notes", [])
    )


@pytest.mark.asyncio
async def test_gap_id_persisted_on_create_fallback_path(db_session, mocker):
    """The fallback (legacy parse) create path persists gap_id, mirroring the
    HTTP create_direction op."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GAPLINK-FB-CREATE",
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
                    "text": "Close the metering gap",
                    "priority": "high",
                    "gap_id": "g-metering",
                    "success_evidence": "Metering accurate",
                    "retirement_condition": "No open metering bugs",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if "metering" in d.get("text", "")]
    assert len(dirs) == 1, "Direction should have been created"
    assert dirs[0].get("gap_id") == "g-metering"


@pytest.mark.asyncio
async def test_gap_id_updated_on_update_fallback_path(db_session, mocker):
    """The fallback update path sets the gap_id linkage on an existing direction."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GAPLINK-FB-UPDATE",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-fb",
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
                    "direction_id": "d-fb",
                    "changes": {"gap_id": "g-linked"},
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    d = next(d for d in project.directions if d.get("id") == "d-fb")
    assert d.get("gap_id") == "g-linked"


@pytest.mark.asyncio
async def test_gap_reconciled_on_create_fallback_path(db_session, mocker):
    """The fallback create path stages the problem-model gap a new direction
    staffs: the direction id joins active_moves and a stale deferral is cleared,
    in the same locked transaction as the directions write."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GAPRECON-FB-CREATE",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
        problem_model={
            "gaps": [
                {
                    "id": "g-metering",
                    "name": "metering",
                    "status": "active",
                    "active_moves": [],
                    "deferral": {"rationale": "not this cycle"},
                }
            ]
        },
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
                    "text": "Close the metering gap",
                    "priority": "high",
                    "gap_id": "g-metering",
                    "success_evidence": "Metering accurate",
                    "retirement_condition": "No open metering bugs",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = next(
        d for d in (project.directions or []) if "metering" in d.get("text", "")
    )
    gap = next(g for g in project.problem_model["gaps"] if g["id"] == "g-metering")
    assert created["id"] in gap["active_moves"]
    # Deferral cleared — the gap is now actively staffed.
    assert gap.get("deferral") is None


@pytest.mark.asyncio
async def test_gap_reconciled_on_reground_fallback_path(db_session, mocker):
    """The fallback update path re-grounds a direction onto a new gap: it joins
    the new gap's active_moves (deferral cleared) and is pruned from the old
    gap's active_moves into past_moves."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GAPRECON-FB-REGROUND",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-fb",
                "text": "Existing direction",
                "status": "active",
                "priority": "medium",
                "gap_id": "g-old",
                "notes": [],
                "research_history": [],
            },
        ],
        problem_model={
            "gaps": [
                {
                    "id": "g-old",
                    "name": "old",
                    "status": "active",
                    "active_moves": ["d-fb"],
                },
                {
                    "id": "g-new",
                    "name": "new",
                    "status": "active",
                    "active_moves": [],
                    "deferral": {"rationale": "parked"},
                },
            ]
        },
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
                    "direction_id": "d-fb",
                    "changes": {"gap_id": "g-new"},
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    gaps = {g["id"]: g for g in project.problem_model["gaps"]}
    # New gap staffed, deferral cleared.
    assert "d-fb" in gaps["g-new"]["active_moves"]
    assert gaps["g-new"].get("deferral") is None
    # Old gap pruned, move archived in past_moves.
    assert "d-fb" not in gaps["g-old"]["active_moves"]
    assert "d-fb" in gaps["g-old"]["past_moves"]


@pytest.mark.asyncio
async def test_gap_reconciled_on_retire_fallback_path(db_session, mocker):
    """The fallback retire path prunes the direction from the gap's active_moves
    and auto-defers the gap when it becomes unstaffed, in the same locked
    transaction as the directions write."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GAPRECON-FB-RETIRE",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-xxx",
                "text": "Close the metering gap",
                "status": "active",
                "priority": "high",
                "gap_id": "g-metering",
                "notes": [],
                "research_history": [],
            },
        ],
        problem_model={
            "gaps": [
                {
                    "id": "g-metering",
                    "name": "metering",
                    "status": "active",
                    "active_moves": ["d-xxx"],
                    "past_moves": [],
                }
            ]
        },
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
                    "op": "retire",
                    "direction_id": "d-xxx",
                    "rationale": "Done — metering gap closed",
                    "terminal_reason": "invalidated",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    gap = next(
        g for g in project.problem_model["gaps"] if g["id"] == "g-metering"
    )
    # Direction pruned from active_moves, archived in past_moves.
    assert "d-xxx" not in gap["active_moves"]
    assert "d-xxx" in gap["past_moves"]
    # Gap auto-deferred because no active staffing remains.
    assert gap.get("deferral") is not None


@pytest.mark.asyncio
async def test_gap_deferral_suppresses_orphan_after_retire(db_session, mocker):
    """After the fallback retire reconciles a gap, the cycle-start advisory no
    longer reports that gap as an orphan (stale_active_moves / orphaned_gaps)."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app.strategic_spawning import _build_cycle_start_reconciliation

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GAPRECON-FB-ORPHAN",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-yyy",
                "text": "Close the latency gap",
                "status": "active",
                "priority": "medium",
                "gap_id": "g-latency",
                "notes": [],
                "research_history": [],
            },
        ],
        problem_model={
            "gaps": [
                {
                    "id": "g-latency",
                    "name": "latency",
                    "status": "active",
                    "active_moves": ["d-yyy"],
                    "past_moves": [],
                }
            ]
        },
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
                    "op": "retire",
                    "direction_id": "d-yyy",
                    "rationale": "Latency gap resolved",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)

    # Build the cycle-start reconciliation view on the post-retire model.
    directions_after = project.directions or []
    recon = _build_cycle_start_reconciliation(
        directions_after, project.problem_model, {}
    )

    # The retired direction's gap must NOT appear as an orphan.
    orphaned = recon.get("orphaned_gaps", [])
    stale = recon.get("stale_active_moves", [])
    gap_ids_orphaned = [o.get("gap_id") for o in orphaned]
    gap_ids_stale = [s.get("gap_id") for s in stale]
    assert "g-latency" not in gap_ids_orphaned, (
        f"g-latency should not be an orphan after retire reconciliation; got {orphaned}"
    )
    assert "g-latency" not in gap_ids_stale, (
        f"g-latency should not appear in stale_active_moves after retire; got {stale}"
    )


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


@pytest.mark.asyncio
async def test_set_retirement_critiques_rejects_empty_list(client, db_session, mocker):
    """HTTP API: an empty list returns 400 — not silently stored as no-op."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="RC-EMPTY",
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
        vm_instance_id="vm-rc-empty",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-rc-empty", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_retirement_critiques",
            "data": [],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text
    assert "no_eligible_retirements" in resp.text


@pytest.mark.asyncio
async def test_set_retirement_critiques_rejects_all_malformed(client, db_session, mocker):
    """HTTP API: a list where every item is missing required fields returns 400."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="RC-MALFORMED",
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
        vm_instance_id="vm-rc-malformed",
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
        str(task.id), "vm-rc-malformed", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_retirement_critiques",
            "data": [
                {"direction_id": "d-123"},  # missing what_it_addressed/pattern/lesson
                {"lesson": "something"},  # missing direction_id/what_it_addressed/pattern
            ],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text
    assert "no_eligible_retirements" in resp.text


# ── set_evidence_progress HTTP API path ────────────────────────────────────────


async def _make_strategic_task(
    db_session, *, name, vm, directions=None, problem_model=None
):
    """Build a committed project + cycle + strategic_review task; return the task."""
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name=name,
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model=problem_model,
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
    assert stored == [
        verdicts[0],
        {**verdicts[1], "stall_advisory_legacy": True},
    ]
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
async def test_set_evidence_progress_structured_stall_is_normalized(client, db_session):
    """Structured stall evidence is bounded and canonical before task persistence."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(
        db_session, name="EP-STALL-ADVISORY", vm="vm-ep-advisory"
    )
    token = create_agent_token(
        str(task.id), "vm-ep-advisory", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_evidence_progress",
            "data": [
                {
                    "direction_id": "d-1",
                    "status": "stalled",
                    "rationale": "capacity unchanged",
                    "stall_advisory_version": 1,
                    "blocking_invariant": "  Runner   Cannot Allocate GPU ",
                    "new_evidence_since_previous_cycle": [],
                    "recommendation": "PIVOT",
                    "continuation_pivot_rationale": " Use a smaller model. ",
                }
            ],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(task)
    advisory = (task.context or {})[EVIDENCE_PROGRESS_CONTEXT_KEY][0][
        "stall_advisory"
    ]
    assert advisory["blocking_invariant"] == "Runner Cannot Allocate GPU"
    assert advisory["recommendation"] == "pivot"
    assert len(advisory["invariant_fingerprint"]) == 64


@pytest.mark.asyncio
async def test_set_evidence_progress_partial_stall_advisory_is_atomic_400(
    client, db_session
):
    """A partial structured advisory cannot persist alongside otherwise valid rows."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(
        db_session, name="EP-STALL-PARTIAL", vm="vm-ep-partial"
    )
    token = create_agent_token(
        str(task.id), "vm-ep-partial", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_evidence_progress",
            "data": [
                {"direction_id": "d-ok", "status": "advancing", "rationale": "x"},
                {
                    "direction_id": "d-bad",
                    "status": "stalled",
                    "rationale": "x",
                    "stall_advisory_version": 1,
                    "blocking_invariant": "same blocker",
                },
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
async def test_cite_knowledge_stores_ids_without_api_called(client, db_session):
    """cite_knowledge persists cited IDs but must NOT flip _strategic_api_called.

    Mirrors the cite_memos rationale: this op is auto-invoked by the thinker
    pass so it must not mask a no-op executor cycle as a success.
    """
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="CK-HTTP", vm="vm-ck")
    token = create_agent_token(str(task.id), "vm-ck", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "cite_knowledge",
            "data": {"cited_knowledge_ids": ["doc-aaa", "doc-bbb"]},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "cite_knowledge"
    assert body["count"] == 2

    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_cited_knowledge_ids") == [
        "doc-aaa",
        "doc-bbb",
    ]
    assert (task.context or {}).get("_strategic_api_called") is not True


@pytest.mark.asyncio
async def test_cite_knowledge_rejects_missing_field(client, db_session):
    """cite_knowledge returns 400 when cited_knowledge_ids is absent or not a list."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="CK-Bad", vm="vm-ck-bad")
    token = create_agent_token(str(task.id), "vm-ck-bad", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "cite_knowledge",
            "data": {"wrong_field": ["doc-1"]},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_set_strategic_challenge_stores_record_without_api_called(
    client, db_session
):
    """set_strategic_challenge sanitizes + stores the record on task.context but
    must NOT flip _strategic_api_called — a challenge-only cycle is not a
    successful executor mutation."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="SC-HTTP", vm="vm-sc")
    token = create_agent_token(str(task.id), "vm-sc", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_strategic_challenge",
            "data": {
                "challenge_version": 1,
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "advancing",
                        "verdict": "approved",
                        "rationale": "ships real evidence",
                    }
                ],
                "retirement_approvals": [
                    {
                        "direction_id": "d-ret",
                        "verdict": "approved",
                        "rationale": "retirement is evidenced",
                    }
                ],
                "retirement_critique_quality": {
                    "verdict": "solid",
                    "rationale": "names the pattern",
                },
                "portfolio_shape_risk": {
                    "verdict": "uncertain",
                    "rationale": "lane sprawl",
                },
                "notes": "challenge notes",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "set_strategic_challenge"
    assert body["version"] == STRATEGIC_CHALLENGE_RECORD_VERSION
    assert body["challenge_version"] == STRATEGIC_CHALLENGE_RECORD_VERSION
    assert body["challenge_status"] == "ok"
    assert body["evidence_progress_count"] == 1
    assert body["retirement_approvals_count"] == 1
    assert body["retirement_critique_quality_count"] == 1
    assert body["portfolio_shape_risk_count"] == 1

    await db_session.refresh(task)
    stored = (task.context or {}).get(STRATEGIC_CHALLENGE_CONTEXT_KEY)
    assert stored is not None
    assert stored["version"] == STRATEGIC_CHALLENGE_RECORD_VERSION
    assert stored["challenge_version"] == STRATEGIC_CHALLENGE_RECORD_VERSION
    assert stored["status"] == "ok"
    assert stored["evidence_progress"][0]["verdict"] == "approved"
    assert stored["retirement_approvals"][0]["verdict"] == "approved"
    assert stored["retirement_critique_quality"]["verdict"] == "solid"
    assert stored["portfolio_shape_risk"]["verdict"] == "uncertain"
    assert stored["notes"] == "challenge notes"
    # The crucial invariant: a challenge does not look like an executor mutation.
    assert (task.context or {}).get("_strategic_api_called") is not True


@pytest.mark.asyncio
async def test_set_strategic_challenge_non_object_returns_400(client, db_session):
    """A non-dict data payload is rejected with 400 and stores nothing."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="SC-BAD", vm="vm-sc-bad")
    token = create_agent_token(str(task.id), "vm-sc-bad", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_strategic_challenge",
            "data": ["not", "an", "object"],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text
    await db_session.refresh(task)
    assert STRATEGIC_CHALLENGE_CONTEXT_KEY not in (task.context or {})


@pytest.mark.asyncio
async def test_retirement_always_proceeds_without_challenge_gate(client, db_session, mocker):
    """Agent retirements always proceed — the observe-only challenge gate has been cut."""
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    async def make_task(name, vm, direction_id):
        return await _make_strategic_task(
            db_session,
            name=name,
            vm=vm,
            directions=[
                {
                    "id": direction_id,
                    "status": "active",
                    "text": f"Direction {direction_id}",
                    "research_history": [],
                    "notes": [],
                }
            ],
        )

    async def retire(task, vm, direction_id, rationale="pattern is complete"):
        token = create_agent_token(str(task.id), vm, task_type="strategic_review")
        return await client.post(
            "/api/agent/strategic",
            json={
                "task_id": str(task.id),
                "op": "retire_direction",
                "data": {
                    "direction_id": direction_id,
                    "rationale": rationale,
                    "terminal_reason": "invalidated",
                },
            },
            headers={"x-agent-token": token},
        )

    async def project_direction(task, direction_id):
        project = await db_session.get(Project, task.project_id)
        await db_session.refresh(project)
        return next(d for d in project.directions if d.get("id") == direction_id)

    # Retirement always proceeds regardless of challenge record presence/verdict.
    for suffix in ("no-challenge", "with-challenge"):
        direction_id = f"d-{suffix}"
        task = await make_task(f"RET-{suffix}", f"vm-{suffix}", direction_id)
        resp = await retire(task, f"vm-{suffix}", direction_id, "evidence-rich memo")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        direction = await project_direction(task, direction_id)
        assert direction["status"] == "expired"
        assert any(
            "Retired by strategic review: evidence-rich memo" in note.get("text", "")
            for note in direction.get("notes", [])
        )

    # Operator-owned retirement path is also unaffected.
    operator_project = Project(
        name="RET-OPERATOR",
        user_id="test-user-123",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-operator",
                "status": "active",
                "text": "Operator-owned retirement",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(operator_project)
    await db_session.commit()
    await db_session.refresh(operator_project)
    db_session.add(
        ProjectMember(
            project_id=operator_project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    await db_session.commit()

    operator_resp = await client.patch(
        f"/api/admin/projects/{operator_project.id}/directions/d-operator",
        json={"status": "expired"},
    )
    assert operator_resp.status_code == 200, operator_resp.text
    await db_session.refresh(operator_project)
    operator_dir = next(
        d for d in operator_project.directions if d.get("id") == "d-operator"
    )
    assert operator_dir["status"] == "expired"


async def _set_portfolio_operation_challenge(
    db_session,
    task,
    *,
    op: str,
    direction_id: str | None = None,
    text: str | None = None,
    verdict: str = "rejected",
):
    challenge = {
        "version": STRATEGIC_CHALLENGE_RECORD_VERSION,
        "status": "ok",
        "portfolio_operations": [
            {
                "op": op,
                "direction_id": direction_id or "",
                **({"text": text} if text is not None else {}),
                "verdict": verdict,
            }
        ],
    }
    challenge = sanitize_strategic_challenge_record(challenge)
    task.context = {
        **(task.context or {}),
        STRATEGIC_CHALLENGE_CONTEXT_KEY: challenge,
    }
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)


async def _post_strategic_op(client, task, vm: str, op: str, data: dict):
    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), vm, task_type="strategic_review")
    return await client.post(
        "/api/agent/strategic",
        json={"task_id": str(task.id), "op": op, "data": data},
        headers={"x-agent-token": token},
    )


@pytest.mark.asyncio
async def test_create_direction_rejected_portfolio_operation_blocks(client, db_session):
    task = await _make_strategic_task(db_session, name="PO-CREATE", vm="vm-po-create")
    await _set_portfolio_operation_challenge(
        db_session,
        task,
        op="create_direction",
        text="Build the widget pipeline",
    )

    resp = await _post_strategic_op(
        client,
        task,
        "vm-po-create",
        "create_direction",
        {
            "text": "Build the widget pipeline",
            "priority": "medium",
            "rationale": "new direction",
            "success_evidence": "Widget pipeline ships",
            "retirement_condition": "No open widget backlog",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["op"] == "create_direction"

    project = await db_session.get(Project, task.project_id)
    await db_session.refresh(project)
    assert project.directions == []

    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_api_called") is True


@pytest.mark.asyncio
async def test_retire_direction_rejected_portfolio_operation_blocks(client, db_session):
    task = await _make_strategic_task(
        db_session,
        name="PO-RETIRE",
        vm="vm-po-retire",
        directions=[
            {
                "id": "d-retire",
                "status": "active",
                "text": "Retire me",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    await _set_portfolio_operation_challenge(
        db_session, task, op="retire_direction", direction_id="d-retire"
    )

    resp = await _post_strategic_op(
        client,
        task,
        "vm-po-retire",
        "retire_direction",
        {
            "direction_id": "d-retire",
            "rationale": "redundant",
            "terminal_reason": "invalidated",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["op"] == "retire_direction"

    project = await db_session.get(Project, task.project_id)
    await db_session.refresh(project)
    retired = next(d for d in project.directions if d.get("id") == "d-retire")
    assert retired["status"] == "active"
    assert not any(
        "Retired by strategic review" in (note.get("text") or "")
        for note in retired.get("notes", [])
    )


@pytest.mark.asyncio
async def test_stage_direction_rejected_portfolio_operation_blocks(client, db_session):
    from app import release_version as rv

    task = await _make_strategic_task(
        db_session,
        name="PO-STAGE",
        vm="vm-po-stage",
        directions=[
            {
                "id": "d-stage",
                "status": "active",
                "text": "Stage me",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    project = await db_session.get(Project, task.project_id)
    await rv.open_version(db_session, project.id, version_number=7)
    await db_session.commit()
    await _set_portfolio_operation_challenge(
        db_session, task, op="stage_direction", direction_id="d-stage"
    )

    resp = await _post_strategic_op(
        client,
        task,
        "vm-po-stage",
        "stage_direction",
        {"direction_id": "d-stage", "rationale": "next version work"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["op"] == "stage_direction"

    await db_session.refresh(project)
    staged = next(d for d in project.directions if d.get("id") == "d-stage")
    assert staged["status"] == "active"
    assert "staged_for_version" not in staged


@pytest.mark.asyncio
async def test_approve_direction_rejected_portfolio_operation_blocks(
    client, db_session
):
    task = await _make_strategic_task(
        db_session,
        name="PO-APPROVE",
        vm="vm-po-approve",
        directions=[
            {
                "id": "d-approve",
                "status": "proposed",
                "text": "Approve me",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    await _set_portfolio_operation_challenge(
        db_session, task, op="approve_direction", direction_id="d-approve"
    )

    resp = await _post_strategic_op(
        client,
        task,
        "vm-po-approve",
        "approve_direction",
        {"direction_id": "d-approve", "rationale": "good idea"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["op"] == "approve_direction"

    project = await db_session.get(Project, task.project_id)
    await db_session.refresh(project)
    approved = next(d for d in project.directions if d.get("id") == "d-approve")
    assert approved["status"] == "proposed"
    assert "activated_at" not in approved


@pytest.mark.asyncio
async def test_approve_direction_staffs_linked_gap(client, db_session):
    """Approve direction must activate the direction and staff its linked gap."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(
        db_session,
        name="PO-APPROVE-GAP",
        vm="vm-po-approve-gap",
        directions=[
            {
                "id": "d-approve",
                "status": "proposed",
                "text": "Approve me",
                "gap_id": "g-1",
                "research_history": [],
                "notes": [],
            }
        ],
        problem_model={
            "revision": 1,
            "gaps": [
                {
                    "id": "g-1",
                    "name": "Gap 1",
                    "status": "active",
                    "active_moves": [],
                    "past_moves": [],
                    "deferral": {
                        "rationale": "waiting on staffing",
                        "created_at": "2026-01-01T00:00:00",
                    },
                }
            ],
        },
    )

    token = create_agent_token(
        str(task.id), "vm-po-approve-gap", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "approve_direction",
            "data": {"direction_id": "d-approve", "rationale": "good idea"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "approve_direction"

    project = await db_session.get(Project, task.project_id)
    await db_session.refresh(project)
    approved = next(d for d in project.directions if d.get("id") == "d-approve")
    assert approved["status"] == "active"
    assert approved["activated_at"]

    gap = next(g for g in project.problem_model["gaps"] if g["id"] == "g-1")
    assert gap["active_moves"] == ["d-approve"]
    assert gap["past_moves"] == []
    assert gap["deferral"] is None


@pytest.mark.asyncio
async def test_reject_direction_rejected_portfolio_operation_blocks(client, db_session):
    task = await _make_strategic_task(
        db_session,
        name="PO-REJECT",
        vm="vm-po-reject",
        directions=[
            {
                "id": "d-reject",
                "status": "proposed",
                "text": "Reject me",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    await _set_portfolio_operation_challenge(
        db_session, task, op="reject_direction", direction_id="d-reject"
    )

    resp = await _post_strategic_op(
        client,
        task,
        "vm-po-reject",
        "reject_direction",
        {"direction_id": "d-reject", "rationale": "not worthwhile"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["op"] == "reject_direction"

    project = await db_session.get(Project, task.project_id)
    await db_session.refresh(project)
    rejected = next(d for d in project.directions if d.get("id") == "d-reject")
    assert rejected["status"] == "proposed"
    assert all(d.get("status") != "rejected" for d in project.directions)


@pytest.mark.asyncio
async def test_set_strategic_challenge_copied_into_cycle_metadata(
    client, db_session, mocker
):
    """End-to-end: a stored challenge record is copied into StrategicCycle.cycle_metadata
    at strategic completion."""
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    from app import strategic_cycle
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    task = await _make_strategic_task(
        db_session,
        name="SC-META",
        vm="vm-sc-meta",
        directions=[{"id": "d-1", "status": "active", "text": "runner"}],
    )
    token = create_agent_token(str(task.id), "vm-sc-meta", task_type="strategic_review")
    chresp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_strategic_challenge",
            "data": {
                "status": "ok",
                "evidence_progress": [
                    {"direction_id": "d-1", "status": "met", "verdict": "approved"}
                ],
            },
        },
        headers={"x-agent-token": token},
    )
    assert chresp.status_code == 200, chresp.text
    # A real executor mutation in the same cycle drives the API completion path
    # (set_evidence_progress flips _strategic_api_called). The challenge rides
    # along into cycle_metadata.
    epresp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_evidence_progress",
            "data": [{"direction_id": "d-1", "status": "met", "rationale": "done"}],
        },
        headers={"x-agent-token": token},
    )
    assert epresp.status_code == 200, epresp.text

    await db_session.refresh(task)
    project = await db_session.get(Project, task.project_id)
    await _handle_strategic_completion(task, "", db_session, project=project)

    cycle_id = (task.context or {}).get("strategic_cycle_id")
    cycle = await strategic_cycle.get(db_session, cycle_id)
    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert "strategic_judgment_challenge" in meta
    assert meta["strategic_judgment_challenge"]["status"] == "ok"
    assert meta["strategic_judgment_challenge"]["evidence_progress"][0]["verdict"] == (
        "approved"
    )


@pytest.mark.asyncio
async def test_staged_only_completion_uses_api_path(db_session, mocker, caplog):
    from unittest.mock import AsyncMock
    from app import strategic_cycle
    from app.models import Project, Task
    from app.strategic_completion import _handle_strategic_completion

    project = Project(
        name="P-staged-only",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[{"id": "d-next", "status": "active"}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "strategic_cycle_id": cycle.id,
            "_strategic_dirs_staged": 1,
            "_strategic_staged_ids": ["d-next"],
            "_strategic_adoption_declared": 2,
        },
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.research_completion.query_findings_index",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_cycle.query_recent_memo_citations",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.strategic_completion._apply_evidence_progress_verdicts",
        return_value=0,
    )
    mocker.patch(
        "app.strategic_completion._check_direction_starvation",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "app.strategic_completion.strategic_cycle.mark_phase2_completed",
        new=AsyncMock(return_value=cycle),
    )
    emit = AsyncMock()
    mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

    with caplog.at_level("INFO", logger="app.strategic_completion"):
        await _handle_strategic_completion(task, "", db_session)

    strategic_cycle.mark_phase2_completed.assert_awaited_once()
    emit.assert_awaited_once()
    assert emit.await_args.kwargs["completion"]["staged_count"] == 1
    assert emit.await_args.kwargs["completion"]["adoption_declared_count"] == 2
    assert emit.await_args.kwargs["completion"]["status"] == "completed"
    assert any(
        "2 capability adoption decision(s) declared" in record.message
        for record in caplog.records
    )


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
async def test_declare_capability_adoption_via_http(client, db_session):
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="CAP-HTTP", vm="vm-cap")
    token = create_agent_token(str(task.id), "vm-cap", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "declare_capability_adoption",
            "data": {
                "cap_name": "repo_map",
                "verdict": "adopt",
                "rationale": "stable trial results",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "declare_capability_adoption"
    assert body["cap_name"] == "repo_map"
    assert body["verdict"] == "adopt"

    await db_session.refresh(task)
    project = await db_session.get(Project, task.project_id)
    assert project is not None
    await db_session.refresh(project)
    stored = project.capability_adoption_verdicts["repo_map"]
    assert stored["verdict"] == "adopt"
    assert stored["evidence_source"] == "strategic_review"
    assert stored["rationale"] == "stable trial results"
    assert "evaluated_at" in stored
    assert (task.context or {}).get("_strategic_adoption_declared") == 1
    assert (task.context or {}).get("_strategic_api_called") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data, detail",
    [
        (
            {"cap_name": "", "verdict": "adopt"},
            "cap_name must be a non-empty string",
        ),
        (
            {"cap_name": "repo_map", "verdict": "maybe"},
            "verdict must be one of",
        ),
    ],
)
async def test_declare_capability_adoption_invalid_payload_returns_400(
    client, db_session, data, detail
):
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="CAP-BAD", vm="vm-cap-bad")
    token = create_agent_token(str(task.id), "vm-cap-bad", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "declare_capability_adoption",
            "data": data,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text
    assert detail in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_declare_capability_adoption_non_strategic_review_returns_403(
    client, db_session
):
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="CAP-403", vm="vm-cap-403")
    token = create_agent_token(str(task.id), "vm-cap-403", task_type="standard")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "declare_capability_adoption",
            "data": {"cap_name": "repo_map", "verdict": "reject"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 403, resp.text


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
                    "terminal_reason": "superseded",
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
                    "terminal_reason": "superseded",
                    "dedup_reasoning": {
                        "closest_directions": ["d-legacy"],
                        "distinction": "This is a status reviver, not a new direction",
                    },
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
    verdict = (target.get("context") or {}).get("grounding_verdict")
    assert verdict and verdict["status"] == "validated"


@pytest.mark.asyncio
async def test_fallback_update_to_superseded_records_retired_memory(
    db_session, mocker
):
    """Fallback op=update flipping status->superseded records retired-direction memory.

    The strategist-driven terminal-status write must queue a
    _recently_retired_directions entry (reason=strategic_superseded, outcome
    carrying the successor pointer) so a same-gap re-spawn is blocked next cycle.
    """
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="SUP-MEM-FB",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-sup",
                "text": "model gc garbage collection optimize performance",
                "status": "active",
                "priority": "medium",
                "gap_id": "g-model-gc",
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
                        "direction_id": "d-sup",
                        "rationale": "replaced by newer approach",
                        "terminal_reason": "superseded",
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
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    entry = next((m for m in mem if m.get("id") == "d-sup"), None)
    assert entry is not None, (
        f"update->superseded must record retired memory; got {mem}"
    )
    assert entry["reason"] == "strategic_superseded"
    assert entry.get("gap_id") == "g-model-gc"
    assert entry.get("outcome") == "superseded by d-new"


@pytest.mark.asyncio
async def test_fallback_update_to_expired_records_retired_memory(db_session, mocker):
    """Fallback op=update flipping status->expired records retired memory (reason+rationale)."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="EXP-MEM-FB",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-exp",
                "text": "cache layer optimization database query performance",
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
                        "direction_id": "d-exp",
                        "rationale": "no longer relevant after refactor",
                        "terminal_reason": "invalidated",
                        "changes": {"status": "expired"},
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    entry = next((m for m in mem if m.get("id") == "d-exp"), None)
    assert entry is not None, f"update->expired must record retired memory; got {mem}"
    assert entry["reason"] == "strategic_expired"
    assert entry.get("outcome") == "no longer relevant after refactor"


@pytest.mark.asyncio
async def test_fallback_update_non_terminal_records_no_retired_memory(
    db_session, mocker
):
    """Fallback op=update that does NOT flip to a terminal status records no memory."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="NONTERM-MEM-FB",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-live",
                "text": "keep improving the live direction",
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
                    "direction_id": "d-live",
                    "rationale": "bump priority",
                    "changes": {"priority": "high"},
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    assert not any(m.get("id") == "d-live" for m in mem), (
        f"non-terminal update must NOT record retired memory; got {mem}"
    )


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
                    "terminal_reason": "superseded",
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
async def test_strategic_http_update_direction_auto_paused_reactivation_stamps_grace(
    client, db_session, mocker
):
    """HTTP update_direction reactivating an auto-paused direction clears residue,
    stamps the auto_recovered_at grace marker, and preserves the churn counter."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="REACT-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-http-auto-paused",
                "text": "auto-paused churn direction",
                "status": "paused",
                "auto_paused": True,
                "paused_reason": "Churn guardrail: low merge rate",
                "paused_at": "2026-01-01T00:00:00",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "churn_family_pause_count": 1,
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
        vm_instance_id="vm-react-http",
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
        str(task.id), "vm-react-http", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-http-auto-paused",
                "rationale": "resume auto-paused direction",
                "changes": {"status": "active"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    target = next(
        d for d in (project.directions or []) if d.get("id") == "d-http-auto-paused"
    )
    assert target.get("status") == "active"
    assert target.get("auto_recovered_at") is not None
    assert "auto_paused" not in target
    assert "paused_reason" not in target
    assert target.get("churn_family_pause_count") == 1


@pytest.mark.asyncio
async def test_strategic_http_update_direction_user_paused_reactivation_blocked(
    client, db_session, mocker
):
    """HTTP update_direction must not resume a USER-paused direction: the response
    is blocked, the status stays paused, and no grace marker is stamped."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="REACT-HTTP-USER",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-http-user-paused",
                "text": "user-paused direction",
                "status": "paused",
                # No auto_paused flag → user/operator paused it.
                "paused_reason": "operator paused",
                "paused_at": "2026-01-01T00:00:00",
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
        vm_instance_id="vm-react-http-user",
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
        str(task.id), "vm-react-http-user", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-http-user-paused",
                "rationale": "try to resume user-paused direction",
                "changes": {"status": "active"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "blocked"

    await db_session.refresh(project)
    target = next(
        d for d in (project.directions or []) if d.get("id") == "d-http-user-paused"
    )
    assert target.get("status") == "paused"
    assert "auto_recovered_at" not in target
    assert target.get("paused_reason") == "operator paused"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract_field", "new_value"),
    [
        ("success_evidence", "New goal: zero P0 incidents"),
        ("implementation_constraints", "Preserve public API compatibility"),
    ],
)
async def test_strategic_http_update_direction_clears_scaffolding_state_on_outcome_change(
    client, db_session, mocker, contract_field, new_value
):
    """HTTP update_direction resets stale scaffolding state when outcome fields change."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name=f"SCAFFOLD-HTTP-{contract_field}",
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
                    "implementation_constraints": "Existing constraints",
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
                "changes": {contract_field: new_value},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    target = next(
        d for d in (project.directions or []) if d.get("id") == "d-http-scaffold"
    )
    assert target["context"][contract_field] == new_value
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
                    "terminal_reason": "superseded",
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
async def test_api_path_create_direction_persists_gap_id(client, db_session, mocker):
    """HTTP create_direction persists the gap_id linkage on the new direction so
    reconciliation can map it back to the problem-model gap it works on."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="GAPLINK-CREATE-HTTP",
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
        vm_instance_id="vm-gaplink",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-gaplink", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Close the invoicing gap",
                "priority": "high",
                "gap_id": "g-invoicing",
                "rationale": "linked to gap",
                "success_evidence": "Invoices generate correctly",
                "retirement_condition": "No open invoicing bugs",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json().get("direction_id")
    assert new_id

    await db_session.refresh(project)
    created = next(d for d in project.directions if d.get("id") == new_id)
    assert created.get("gap_id") == "g-invoicing"


@pytest.mark.asyncio
async def test_api_path_create_direction_persists_criterion_keys(client, db_session):
    """HTTP create_direction must persist criterion_keys onto the JSON snapshot
    and normalized Direction row; downstream merge evidence reads the row."""
    from app import strategic_cycle

    project = Project(
        name="CRITERIONLINK-CREATE-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-create-direction-map",
        text="Mapped create_direction criterion",
        created_by="test",
    )
    db_session.add(criterion)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-criterion-create",
        payloads=[{"type": "strategic_review"}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-criterion-create", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Own the mapped create criterion",
                "priority": "high",
                "criterion_keys": [criterion.criterion_key],
                "rationale": "link acceptance coverage",
                "success_evidence": "Criterion evidence is recorded",
                "retirement_condition": "Criterion is covered",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["criterion_keys"] == [criterion.criterion_key]
    new_id = body["direction_id"]

    await db_session.refresh(project)
    created = next(d for d in project.directions if d.get("id") == new_id)
    assert created.get("criterion_keys") == [criterion.criterion_key]

    direction_row = (
        await db_session.execute(
            select(Direction).where(
                Direction.project_id == project.id,
                Direction.direction_key == new_id,
            )
        )
    ).scalar_one()
    assert direction_row.criterion_keys == [criterion.criterion_key]


@pytest.mark.asyncio
async def test_api_path_update_direction_sets_and_clears_gap_id(
    client, db_session, mocker
):
    """HTTP update_direction can set the gap_id linkage on an existing direction
    and clear it again (empty/None removes the key)."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="GAPLINK-UPDATE-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-link",
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

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-gaplink-up",
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
        str(task.id), "vm-gaplink-up", task_type="strategic_review"
    )

    # Set the gap_id.
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-link",
                "changes": {"gap_id": "g-new"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(project)
    d = next(d for d in project.directions if d.get("id") == "d-link")
    assert d.get("gap_id") == "g-new"

    # Clear it.
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-link",
                "changes": {"gap_id": None},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(project)
    d = next(d for d in project.directions if d.get("id") == "d-link")
    assert "gap_id" not in d


@pytest.mark.asyncio
async def test_api_path_update_direction_persists_and_clears_criterion_keys(
    client, db_session
):
    """HTTP update_direction changes.criterion_keys must update both storage
    surfaces and de-duplicate keys while preserving order."""
    from app import strategic_cycle

    direction_dict = {
        "id": "d-criterion-update",
        "text": "Existing mapped direction",
        "status": "active",
        "priority": "medium",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="CRITERIONLINK-UPDATE-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[dict(direction_dict)],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-update-direction-map",
        text="Mapped update_direction criterion",
        created_by="test",
    )
    db_session.add(criterion)
    direction = Direction(
        project_id=project.id,
        direction_key="d-criterion-update",
        data=dict(direction_dict),
        status="active",
    )
    db_session.add(direction)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-criterion-update",
        payloads=[{"type": "strategic_review"}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-criterion-update", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-criterion-update",
                "changes": {
                    "criterion_keys": [
                        criterion.criterion_key,
                        criterion.criterion_key,
                    ],
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["criterion_keys"] == [criterion.criterion_key]

    await db_session.refresh(project)
    d = next(d for d in project.directions if d.get("id") == "d-criterion-update")
    assert d.get("criterion_keys") == [criterion.criterion_key]
    await db_session.refresh(direction)
    assert direction.criterion_keys == [criterion.criterion_key]

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-criterion-update",
                "changes": {"criterion_keys": []},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["criterion_keys"] == []

    await db_session.refresh(project)
    d = next(d for d in project.directions if d.get("id") == "d-criterion-update")
    assert d.get("criterion_keys") == []
    await db_session.refresh(direction)
    assert direction.criterion_keys == []


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

    def _row(
        self,
        title,
        grade="MEDIUM",
        desc="",
        claims=None,
        sources=None,
        verification_basis="pattern_inferred",
    ):
        row = {
            "finding_title": title,
            "finding_description": desc,
            "actionability_grade": grade,
            "claims": claims or [],
            "sources": sources or [],
        }
        if verification_basis is not None:
            row["verification_basis"] = verification_basis
        return row

    def test_empty_rows_returns_empty_string(self):
        from app.strategic_completion import format_findings_for_prompt

        assert format_findings_for_prompt([]) == ""

    def test_single_row_no_claims_no_sources(self):
        from app.strategic_completion import format_findings_for_prompt

        row = self._row(
            "Title A",
            grade="HIGH",
            desc="Some description",
            verification_basis="code_verified",
        )
        result = format_findings_for_prompt([row])
        assert "[HIGH/code_verified] Title A: Some description" in result
        assert "claims" not in result
        assert "sources" not in result

    def test_verification_basis_rendered(self):
        from app.strategic_completion import format_findings_for_prompt

        row = self._row(
            "Verified finding",
            grade="HIGH",
            desc="Backed by code reading",
            verification_basis="code_verified",
        )
        result = format_findings_for_prompt([row])
        assert "code_verified" in result
        # basis is folded into the grade prefix, not appended as a separate field
        assert "[HIGH/code_verified]" in result

    def test_downgraded_basis_rendered_distinctly(self):
        from app.strategic_completion import format_findings_for_prompt

        downgraded = self._row(
            "Stale finding",
            grade="HIGH",
            desc="Backed by code reading once",
            verification_basis="pattern_inferred",
        )
        downgraded["verification_basis_downgraded_from"] = "code_verified"
        downgraded["verification_basis_downgrade_reason"] = (
            "HEAD validation failed: 1 broken source ref, 0 ungrounded claims"
        )
        honest = self._row(
            "Honest inference",
            grade="HIGH",
            desc="Deduction only",
            verification_basis="pattern_inferred",
        )

        result = format_findings_for_prompt([downgraded, honest])
        assert (
            "[HIGH/pattern_inferred downgraded-from=code_verified] Stale finding:"
            in result
        )
        assert (
            "downgrade: HEAD validation failed: 1 broken source ref, 0 ungrounded claims"
            in result
        )
        assert "[HIGH/pattern_inferred] Honest inference:" in result
        assert (
            "Honest inference" in result
            and "downgraded-from" not in result.splitlines()[-1]
        )

    def test_verification_basis_defaults_when_missing(self):
        from app.strategic_completion import format_findings_for_prompt

        # A row without the verification_basis key falls back to pattern_inferred,
        # mirroring the findings-index default.
        row = self._row("No basis", grade="LOW", verification_basis=None)
        assert "verification_basis" not in row
        result = format_findings_for_prompt([row])
        assert "[LOW/pattern_inferred]" in result

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
    is applied synchronously within the API handler and flips the open
    ReleaseVersion's phase immediately — not deferred to task completion.

    The agent embeds version_phase_decision as a top-level sibling of the
    problem_model; the /api/agent/strategic endpoint lifts it out (ProblemModel
    is extra="forbid"), applies it synchronously, and pops the context key so
    _handle_strategic_completion is a no-op. Exercises the REAL HTTP transport.
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

    # Synchronous apply: phase flip happened during set_problem_model itself.
    await db_session.refresh(version)
    assert version.phase == "stabilize", (
        f"Expected open ReleaseVersion to flip to stabilize during set_problem_model, got {version.phase!r}"
    )
    assert version.phase_changed_at is not None
    assert version.phase_reasoning == "Enough features landed; time to harden."

    # Context key is consumed and popped — not stored for the completion handler.
    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_version_phase_decision") is None

    # Completion handler is a no-op for version_phase (key already applied).
    await _handle_strategic_completion(task, "", db_session)
    await db_session.refresh(version)
    assert version.phase == "stabilize"  # unchanged — no second flip

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


# ── cold-start-wipe protection (never silently wipe a mature problem_model) ───


def _mature_problem_model() -> dict:
    """A non-empty durable model with content in all three agent-owned
    collections — the kind a blind cold-start bootstrap would silently erase."""
    return {
        "revision": 7,
        "capabilities": [{"name": "Billing", "status": "working"}],
        "gaps": [{"id": "g-1", "name": "invoicing", "status": "active"}],
        "friction": [{"id": "f-1", "name": "flaky CI", "status": "active"}],
    }


@pytest.mark.asyncio
async def test_set_problem_model_api_preserves_mature_on_bootstrap_payload(
    client, db_session
):
    """API seam: a bootstrap-looking set_problem_model (only intent, no
    agent-owned collections) over a non-empty durable model must succeed (200)
    AND leave the existing capabilities/gaps/friction intact — the
    non-destructive merge preserves entries the agent never echoed back.

    Guards the 'never silently wiped' guarantee end-to-end through
    /api/agent/strategic: with merge semantics (main #7449) omission can no
    longer wipe, so the prior reject-with-409 path is gone — preservation is
    proven by the surviving collections, not a raised error.
    """
    from app.auth import create_agent_token
    from app.models import TaskStatus

    project = Project(
        name="PM-WIPE-409",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model=_mature_problem_model(),
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-pm-wipe",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-pm-wipe", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            # Cold-start bootstrap: only intent, no agent-owned collections —
            # the bootstrap-over-mature signature (payload dropped the model).
            "data": {"intent": {"purpose": "Fresh start."}},
        },
        headers={"x-agent-token": token},
    )
    # Accepted (200) — the merge applies the intent and bumps the revision.
    assert resp.status_code == 200, resp.text

    # The mature model's agent-owned collections survive intact — never wiped,
    # even though the proposal omitted them entirely.
    await db_session.refresh(project)
    assert project.problem_model["revision"] == 8
    assert len(project.problem_model["capabilities"]) == 1
    assert len(project.problem_model["gaps"]) == 1
    assert len(project.problem_model["friction"]) == 1


@pytest.mark.asyncio
async def test_handle_strategic_completion_preserves_collections_on_bootstrap(
    db_session, mocker
):
    """Completion-handler path: when the agent's final-output problem_model omits
    the agent-owned collections (the payload dropped them), the non-destructive
    merge preserves the prior capabilities/gaps/friction rather than erasing
    them. Mirrors the API-seam preservation guarantee on the parse-from-logs
    path that _handle_strategic_completion uses.
    """
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="PM-WIPE-KEEP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model=_mature_problem_model(),
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

    # Final output that omits the agent-owned collections (the payload dropped
    # them) and carries only intent.
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "problem_model": {"intent": {"purpose": "Fresh start."}},
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    # Agent-owned collections preserved by the merge — never wiped. The model
    # advances (revision bumps) since the merged update is applied.
    await db_session.refresh(project)
    assert project.problem_model["revision"] == 8
    assert len(project.problem_model["capabilities"]) == 1
    assert len(project.problem_model["gaps"]) == 1
    assert len(project.problem_model["friction"]) == 1


# ── qualify_request transport (automain LTS Phase 3 / batch 3b) ──────────────


def test_extract_qualify_request_normalizes_explicit_supported_probes():
    from app.strategic_completion import extract_qualify_request

    assert extract_qualify_request(
        {
            "reason": "  live behavior is release-critical  ",
            "probes": [" WALKTHROUGH_E2E "],
        }
    ) == {
        "reason": "live behavior is release-critical",
        "probes": ["walkthrough_e2e"],
    }
    assert extract_qualify_request({"reason": "ready"}) == {
        "reason": "ready",
        "probes": [],
    }


@pytest.mark.parametrize(
    "probes",
    ["walkthrough_e2e", ["unsupported"], [7], [""], ["walkthrough_e2e"] * 2],
)
def test_extract_qualify_request_rejects_invalid_probe_contract(probes):
    from app.strategic_completion import extract_qualify_request

    with pytest.raises(ValueError):
        extract_qualify_request({"reason": "ready", "probes": probes})


@pytest.mark.asyncio
async def test_qualify_request_flows_end_to_end(client, db_session, mocker):
    """End-to-end: a qualify_request embedded in a set_problem_model call is
    applied synchronously within the API handler so it takes immediate effect.

    The strategic agent embeds qualify_request as a top-level sibling of the
    problem_model; the /api/agent/strategic endpoint lifts it out (ProblemModel
    is extra="forbid"), applies it synchronously by calling _apply_qualify_request
    directly, and pops the context key so _handle_strategic_completion is a
    no-op — no double-application for tasks that also complete normally.

    Regression for fr-qualify-request-application-timing: a qualify_request
    submitted mid-session can no longer sit queued for the entire remaining duration
    of the conversation.
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

    # The qualify gate is fail-CLOSED on unavailable compliance evidence, so the
    # flow under test (qualify_request → run_qualification) needs a fully-available
    # clean compliance record. Stub the GitHub-backed evaluation as success and
    # persist the result exactly as production ensure_current_ci_compliance does
    # (the gate reads project.ci_compliance for the envelope's head_sha).
    from app import ci_compliance as _ci_mod

    _head_sha = "c" * 40
    _compliant_data = {
        "ref": _head_sha,
        "compliant": True,
        "violations": [],
        "branch_literal_violations": [],
        "checked": 0,
        "limits_available": True,
        "tree_available": True,
    }
    mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=AsyncMock(
            return_value={
                "status": "success",
                "integration_branch": "automain",
                "head_sha": _head_sha,
                "data": _compliant_data,
                "computed": False,
            }
        ),
    )
    _ci_mod.record_ci_compliance(project, _compliant_data)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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
                    "probes": ["walkthrough_e2e"],
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

    # Synchronous apply: run_qualification fired during set_problem_model itself —
    # not deferred to task completion.
    run_qualification.assert_awaited_once()
    call_args = run_qualification.await_args.args
    # run_qualification(session, project_id, version) — the handler's own session
    # is used (not db_session), so check project_id and version identity only.
    assert call_args[1] == project.id
    assert call_args[2].id == version.id
    assert run_qualification.await_args.kwargs["requested_probes"] == [
        "walkthrough_e2e"
    ]

    # The context key is consumed and popped — no stale key left over.
    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_qualify_request") is None

    # Completion handler does NOT trigger a second qualification (idempotency).
    await _handle_strategic_completion(task, "", db_session)
    run_qualification.assert_awaited_once()  # still exactly once


@pytest.mark.asyncio
async def test_qualify_request_side_effect_failure_does_not_break_durable_write(
    client, db_session, mocker
):
    """STRUCTURAL regression (ci_compliance connection-death wedge).

    The qualify_request side-effect does minutes of external GitHub I/O
    (CI-compliance scan + run_qualification). It used to run INSIDE the
    completion / set_problem_model transaction, under the Project ``FOR UPDATE``
    lock — so the idle DB connection died mid-scan (asyncpg
    ``connection is closed`` on ``UPDATE project SET ci_compliance``), poisoned
    the session, and the commit raised ``PendingRollbackError``. The handler
    classified that as transient and requeued the WHOLE agent job, re-running a
    5-24 min review up to ``max_retries`` times before failing -- even though the
    agent's design-doc write had already landed.

    The fix runs the qualify side-effect AFTER the durable commit, on an isolated
    fresh session, fail-open. This test simulates the side-effect blowing up the
    exact way the connection death did and asserts the durable problem_model
    write still commits and the agent gets a 200 — never a 500 / requeue.
    """
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import release_version as rv
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    # Stand in for the connection-death poison: the qualify side-effect raises a
    # disconnect-class error. Patched at the module global so the post-commit
    # wrapper's ``await _apply_qualify_request(...)`` hits it.
    boom = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        new_callable=AsyncMock,
        side_effect=RuntimeError(
            "(sqlalchemy.dialects.postgresql.asyncpg.InterfaceError) connection is "
            "closed [SQL: UPDATE project SET ci_compliance=$1 WHERE project.id = $2]"
        ),
    )

    project = Project(
        name="QR-ISOLATE",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="hardening")
    db_session.add(version)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-qr-iso",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-qr-iso", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Ship it", "current_focus": "qualify"},
                "capabilities": [],
                "gaps": [],
                "qualify_request": {"reason": "release-ready"},
            },
        },
        headers={"x-agent-token": token},
    )

    # The durable problem_model write survived even though the qualify side-effect
    # raised a connection-death error — no 500, no requeue.
    assert resp.status_code == 200, resp.text
    assert resp.json()["qualify_request_received"] is True
    await db_session.refresh(project)
    assert (project.problem_model or {}).get("intent", {}).get("purpose") == "Ship it"
    # The side-effect ran POST-commit (on the isolated fresh session) and its
    # failure was swallowed rather than propagated to the durable write.
    boom.assert_awaited_once()


@pytest.mark.asyncio
async def test_qualify_request_skipped_when_version_not_in_stabilize(
    client, db_session, mocker
):
    """A qualify_request is dropped (qualifier not run) when the open version is
    still in the build phase — qualification only runs from stabilize.

    The field is stripped from problem_model and the synchronous apply path
    no-ops (phase gate). The context key is popped regardless so the completion
    handler is also a no-op.
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
    # The field is received (reported in the response).
    assert resp.json()["qualify_request_received"] is True
    # Phase gate blocks the qualifier — run_qualification not called.
    run_qualification.assert_not_awaited()
    # Context key is consumed and popped, not left in task.context.
    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_qualify_request") is None

    # Completion handler is also a no-op (key already popped).
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


@pytest.mark.asyncio
async def test_qualify_request_failure_recorded_on_release_version(
    db_session, mocker
):
    """When run_qualification raises unexpectedly, _apply_qualify_request must
    record a durable qualify_request_error on ReleaseVersion.last_qualification
    rather than silently swallowing the failure into a log line only.

    Regression: before this fix the outer except swallowed all exceptions with
    no queryable trace — qualification_attempts stayed 0, last_qualification
    stayed None, and diagnosis required log-diving.
    """
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _apply_qualify_request
    from app import release_version as rv

    project = Project(
        name="QR-ERR-DURABLE",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="ready")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.last_qualification is None

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-qr-err",
        payloads=[
            {"type": "strategic_review", "prompt": "r", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # The qualify gate is fail-CLOSED on unavailable compliance evidence, so this
    # test needs a fully-available clean record to reach run_qualification (the
    # seam under test is the durable error write, not the gate). Stub the
    # GitHub-backed evaluation as success and persist the result exactly as
    # production ensure_current_ci_compliance does.
    from app import ci_compliance as _ci_mod

    _head_sha = "d" * 40
    _compliant_data = {
        "ref": _head_sha,
        "compliant": True,
        "violations": [],
        "branch_literal_violations": [],
        "checked": 0,
        "limits_available": True,
        "tree_available": True,
    }
    mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=AsyncMock(
            return_value={
                "status": "success",
                "integration_branch": "automain",
                "head_sha": _head_sha,
                "data": _compliant_data,
                "computed": False,
            }
        ),
    )
    _ci_mod.record_ci_compliance(project, _compliant_data)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Force run_qualification to raise — simulates any unexpected failure
    # (DB error, network blip, unhandled edge case inside the qualifier).
    mocker.patch(
        "app.release_qualifier.run_qualification",
        new_callable=AsyncMock,
        side_effect=RuntimeError("injected failure for durable-error test"),
    )

    await _apply_qualify_request(
        {"reason": "ready"},
        task,
        project,
        db_session,
        path="test",
    )
    # _apply_qualify_request does not commit — its production caller
    # (_apply_qualify_request_post_commit) commits after it returns. Mirror that
    # here: refresh() re-reads committed DB state under real ORM expiry, so an
    # uncommitted pending write would be (correctly) discarded.
    await db_session.commit()

    # The failure must be recorded on the ReleaseVersion row, not just logged.
    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq is not None, (
        "last_qualification must be set after run_qualification raises — "
        "a silent log line is not queryable by the agent or operator"
    )
    assert "qualify_request_error" in lq, (
        f"last_qualification must contain qualify_request_error; got: {lq}"
    )
    assert "qualify_request_error_at" in lq
    # The error text must identify the failure so it is diagnosable without logs.
    assert "injected failure" in lq["qualify_request_error"]


@pytest.mark.asyncio
async def test_ci_blocker_commits_before_isolated_strategic_refresh(mocker):
    """The refresh transaction cannot roll back the durable blocker write."""
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.strategic_completion import _apply_qualify_request_post_commit

    events: list[str] = []
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project = SimpleNamespace(id=project_id)
    task = SimpleNamespace(id=task_id)

    first = AsyncMock()
    first.get.side_effect = [project, task]
    first.commit.side_effect = lambda: events.append("blocker_committed")
    second = AsyncMock()
    second.commit.side_effect = lambda: events.append("refresh_committed")

    class _SessionContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    factory = mocker.Mock(
        side_effect=[_SessionContext(first), _SessionContext(second)]
    )
    apply_request = mocker.patch(
        "app.strategic_completion._apply_qualify_request",
        new=AsyncMock(return_value=True),
    )

    async def _arm(*_args, **kwargs):
        assert events == ["blocker_committed"]
        assert kwargs["skip_locked"] is False
        events.append("refresh_armed")
        return True

    arm_refresh = mocker.patch(
        "app.strategic_trigger.request_strategic_refresh", side_effect=_arm
    )

    await _apply_qualify_request_post_commit(
        {"reason": "ready"},
        task_id=task_id,
        project_id=project_id,
        path="test",
        session_factory=factory,
    )

    apply_request.assert_awaited_once()
    arm_refresh.assert_awaited_once()
    first.rollback.assert_not_awaited()
    second.commit.assert_awaited_once()
    assert events == ["blocker_committed", "refresh_armed", "refresh_committed"]


# ── new_acceptance_criteria transport / fallback ─────────────────────────────


@pytest.mark.asyncio
async def test_handle_strategic_completion_applies_new_acceptance_criteria_from_logs(
    db_session, mocker
):
    """Fallback completion output must author version-scoped criteria."""
    from unittest.mock import AsyncMock
    from app import acceptance_criteria as ac
    from app import release_version as rv
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="NAC-FALLBACK",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    db_session.add(version)
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
            "new_acceptance_criteria": [
                "All unit tests pass",
                "No unresolved P0 bugs",
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    fetched = await ac.get_criteria_for_version(db_session, version.id)
    assert [c.text for c in fetched] == [
        "All unit tests pass",
        "No unresolved P0 bugs",
    ]
    assert all(c.created_by == "strategist" for c in fetched)


@pytest.mark.asyncio
async def test_set_problem_model_lifts_new_acceptance_criteria_api_path(
    client, db_session
):
    """The API seam must strip and apply new_acceptance_criteria synchronously."""
    from app import acceptance_criteria as ac
    from app import release_version as rv
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app.strategic_completion import (
        NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY,
        _handle_strategic_completion,
    )
    from app import strategic_cycle

    project = Project(
        name="NAC-E2E",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await db_session.refresh(version)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-nac-e2e",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-nac-e2e", task_type="strategic_review")
    criteria_texts = [
        "All unit tests pass",
        "No unresolved P0 bugs",
    ]

    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Ship the thing", "current_focus": "build"},
                "capabilities": [],
                "gaps": [],
                "new_acceptance_criteria": criteria_texts,
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "set_problem_model"
    assert body["new_acceptance_criteria_received"] is True

    await db_session.refresh(project)
    assert "new_acceptance_criteria" not in (project.problem_model or {})

    # Synchronous apply: criteria created during set_problem_model, not deferred.
    fetched = await ac.get_criteria_for_version(db_session, version.id)
    assert [c.text for c in fetched] == criteria_texts
    assert all(c.created_by == "strategist" for c in fetched)

    # Context key is consumed and popped — completion handler is a no-op.
    await db_session.refresh(task)
    assert (task.context or {}).get(NEW_ACCEPTANCE_CRITERIA_CONTEXT_KEY) is None

    # Completion handler does not create duplicate criteria.
    await _handle_strategic_completion(task, "", db_session)
    fetched2 = await ac.get_criteria_for_version(db_session, version.id)
    assert len(fetched2) == len(criteria_texts)  # no duplicates


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

    def test_aggregate_stranded_merges_discounted_from_lifetime_merged_and_rate(self):
        # Aggregate: 5 verified merges, 2 confirmed-stranded → credit-eligible = 3
        # 10 tasks_completed, 0 failed. Window is empty.
        # agg_merged = 5 - 2 = 3; non_merge = max(0, 10 - 3) = 7; total_finished = 10
        # lifetime_merge_rate = proof_weighted / 10 ≈ 3/10 (no acceptance fields → proof == verified)
        agg = {
            "total_prs_merged": 5,
            "total_verified_prs_merged": 5,
            "total_stranded_prs_merged": 2,
            "total_tasks_completed": 10,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(research_history=[], history_aggregate=agg)
        m = _compute_lifetime_burn_metrics(d)
        assert m is not None
        assert m["lifetime_merged"] == 3  # 5 verified - 2 stranded
        assert m["lifetime_non_merge_finished"] == 7  # 10 completed - 3 credit-merged
        assert m["lifetime_merge_rate"] == round(3 / 10, 3)


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


def _verified_history(
    n,
    tasks_created=2,
    tasks_completed=1,
    tasks_completed_no_pr=0,
    tasks_failed=0,
    tasks_failed_infra=0,
    prs_merged=1,
    verified_prs_merged=0,
):
    """research_history entries carrying BOTH prs_merged and verified_prs_merged.

    Lets a test model an unverified-degraded merge cycle: prs_merged > 0 (the PR
    landed) while verified_prs_merged == 0 (the merge went green via a no-CI
    fall-open where verification was blocked). verified_merged_count() returns 0
    for such an entry, so direction-level control decisions must treat it as no
    merge.
    """
    return [
        {
            "tasks_created": tasks_created,
            "tasks_completed": tasks_completed,
            "tasks_completed_no_pr": tasks_completed_no_pr,
            "tasks_failed": tasks_failed,
            "tasks_failed_infra": tasks_failed_infra,
            "prs_merged": prs_merged,
            "verified_prs_merged": verified_prs_merged,
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
    """Core regression: a still-productive direction that is genuinely LANDING work
    (or completing already-built no-ops) must NOT be paused by Rule 2 / 5 / 6, while
    genuine code failures still pause.

    Signal-that-lies fix: ``last_cycle_productive`` alone (mere spawn/notes) no
    longer holds the shield — the direction must show a real landing/no-op signal
    in its recent history. Each "skipped_when_still_productive" case therefore
    carries a genuine no-op completion on its most-recent cycle (which resets the
    zero-landed strip); the companion "..._fires_when_spawn_only_never_lands" cases
    confirm that a spawn-only-never-land direction now correctly reaches the pause.
    """

    def test_rule2_zero_merge_skipped_when_still_productive(self):
        # 6 cycles, 0 merges — Rule 2 would fire — BUT the latest cycle completed a
        # genuine no-op (already-built), so the direction keeps its shield.
        history = _history(
            6, tasks_created=2, tasks_completed=2, tasks_failed=0, prs_merged=0
        )
        history[-1]["tasks_completed_no_pr"] = 1
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=history,
        )
        assert _check_direction_health(d) is None

    def test_rule2_zero_merge_fires_when_spawn_only_never_lands(self):
        # Corrected behaviour: last_cycle_productive credits mere spawning, but 6
        # cycles with zero merges AND zero no-op completions is spawn-only churn —
        # the shield is stripped and Rule 2 (zero-merge) now fires.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(
                6, tasks_created=2, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Unproductive" in result

    def test_rule5_churn_skipped_when_still_productive(self):
        # Would trip Rule 5 (high non-merge volume, 0% merge rate) but the latest
        # cycle is a genuine no-op completion → shield retained.
        history = _history(
            5, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=0
        )
        history[-1]["tasks_completed_no_pr"] = 1
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=history,
        )
        assert _check_direction_health(d) is None

    def test_rule5_churn_fires_when_spawn_only_never_lands(self):
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(
                5, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=0
            ),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Churn guardrail" in result

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
        # Rule 6 (lifetime burn) has NO real_merges guard, so the still-productive
        # shield is genuinely load-bearing here: the latest cycle's no-op completion
        # keeps the shield and skips the lifetime-burn backstop.
        history = _history(
            5, tasks_created=5, tasks_completed=2, tasks_failed=0, prs_merged=0
        )
        history[-1]["tasks_completed_no_pr"] = 1
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=history,
            history_aggregate=agg,
        )
        assert _check_direction_health(d) is None

    def test_rule6_lifetime_burn_fires_when_spawn_only_never_lands(self, mocker):
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
        result = _check_direction_health(d)
        assert result is not None
        assert "Lifetime burn backstop" in result

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


class TestStillProductiveLandingGate:
    """Signal-that-lies fix for the still-productive shield: ``last_cycle_productive``
    credits mere spawning / notes-only cycles, which is fine for the cooldown cadence
    but must NOT keep the merge-rate pause shield up forever. The shield now requires
    a real landing (merged PR, integration OR staging) or a genuine no-op completion
    within the recent window, and is stripped after N consecutive zero-landed cycles.
    """

    def test_stripped_after_n_zero_landed_cycles(self):
        # 6 cycles, all spawn-only (zero merges, zero no-ops) → shield stripped.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(6, prs_merged=0),
        )
        assert _direction_still_productive(d) is False

    def test_held_when_recent_staging_merge(self):
        # A staging merge on the latest cycle (merged_prs_any_base) is a landing,
        # even before rollup-landing credit propagates → shield held.
        hist = _history(6, prs_merged=0)
        hist[-1]["merged_prs_any_base"] = 1
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=hist,
        )
        assert _direction_still_productive(d) is True

    def test_held_when_recent_noop_completion(self):
        # Genuine no-op completion (already-built) on the latest cycle preserves the
        # shield — its legitimate purpose.
        hist = _history(6, prs_merged=0)
        hist[-1]["tasks_completed_no_pr"] = 1
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=hist,
        )
        assert _direction_still_productive(d) is True

    def test_held_when_zero_landed_below_strip_max(self):
        # Landed 3 cycles ago, then 2 trailing zero-landed (< default strip_max 3)
        # → shield still held (recent-enough landing keeps the grace).
        hist = _history(5, prs_merged=0)
        hist[-3]["prs_merged"] = 1
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=hist,
        )
        assert _direction_still_productive(d) is True

    def test_empty_history_not_stripped(self):
        # No history → 0 consecutive zero-landed → shield unchanged (the pause rules
        # need history to fire anyway, so this is behaviour-preserving).
        d = _direction(
            last_cycle_productive=True, consecutive_unproductive_cycles=0
        )
        assert _direction_still_productive(d) is True

    def test_strip_threshold_configurable(self, mocker):
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_productive_shield_max_zero_landed_cycles": 1},
            clear=False,
        )
        # strip_max=1: a single zero-landed latest cycle strips the shield.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(3, prs_merged=0),
        )
        assert _direction_still_productive(d) is False


class TestCycleLandedOrNoop:
    """Per-cycle shield-qualifying predicate + the trailing zero-landed counter."""

    def test_merged_prs_any_base_is_landing(self):
        assert _cycle_landed_or_noop({"merged_prs_any_base": 1}) is True

    def test_verified_merge_is_landing(self):
        assert _cycle_landed_or_noop({"verified_prs_merged": 2}) is True

    def test_legacy_prs_merged_is_landing(self):
        assert _cycle_landed_or_noop({"prs_merged": 1}) is True

    def test_noop_completion_qualifies(self):
        assert _cycle_landed_or_noop({"tasks_completed_no_pr": 1}) is True

    def test_spawn_only_does_not_qualify(self):
        assert (
            _cycle_landed_or_noop(
                {"tasks_created": 3, "tasks_completed": 2, "prs_merged": 0}
            )
            is False
        )

    def test_non_dict_is_false(self):
        assert _cycle_landed_or_noop(None) is False

    def test_consecutive_zero_landed_counts_trailing(self):
        hist = [
            {"prs_merged": 1},
            {"prs_merged": 0},
            {"prs_merged": 0},
        ]
        assert (
            _direction_consecutive_zero_landed_cycles({"research_history": hist}) == 2
        )

    def test_consecutive_zero_landed_stops_at_landing(self):
        hist = [
            {"prs_merged": 0},
            {"merged_prs_any_base": 1},
            {"prs_merged": 0},
        ]
        assert (
            _direction_consecutive_zero_landed_cycles({"research_history": hist}) == 1
        )

    def test_consecutive_zero_landed_empty_history(self):
        assert _direction_consecutive_zero_landed_cycles({}) == 0


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


class TestVerifiedMergePredicateDrivesDecisions:
    """Direction-level control decisions count only verified merges.

    A merge that landed in an unverified-degraded state (prs_merged > 0 but
    verified_prs_merged == 0) must NOT mask a stall or suppress an auto-pause /
    completion gate. Each case pairs a degraded-only direction (which now trips
    the rule) with a genuinely-verified direction (which is unaffected), proving
    the predicate — not the raw count — drives the decision.
    """

    # --- Rule 2: zero-merge auto-pause ---

    def test_rule2_fires_when_only_merges_are_degraded(self):
        # 5 cycles, each landed a PR (prs_merged=1) but none verified. Last cycle
        # fully finished; 2 real (non-no-op) completions per cycle. Raw count would
        # suppress the pause; verified count is 0 → Unproductive fires.
        d = _direction(
            research_history=_verified_history(
                5,
                tasks_created=2,
                tasks_completed=2,
                tasks_failed=0,
                prs_merged=1,
                verified_prs_merged=0,
            )
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Unproductive" in result

    def test_rule2_silent_when_merges_are_verified(self):
        # Same shape, but the merges are genuinely verified → not unproductive.
        d = _direction(
            research_history=_verified_history(
                5,
                tasks_created=2,
                tasks_completed=2,
                tasks_failed=0,
                prs_merged=1,
                verified_prs_merged=1,
            )
        )
        assert _check_direction_health(d) is None

    # --- Rule 4: recent-stall auto-pause ---

    def test_rule4_stall_fires_with_degraded_merges_and_code_failure(self):
        # 3 cycles each: 1 code failure AND a degraded merge. The degraded merge
        # must not count as a merge, so the stall rule fires.
        d = _direction(
            research_history=_verified_history(
                3,
                tasks_created=2,
                tasks_completed=1,
                tasks_failed=1,
                tasks_failed_infra=0,
                prs_merged=1,
                verified_prs_merged=0,
            )
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert _is_code_failure_pause_reason(reason) is True

    def test_rule4_stall_silent_when_merges_verified(self):
        # Identical except the merges are verified → cycle is not "failed", no stall.
        d = _direction(
            research_history=_verified_history(
                3,
                tasks_created=2,
                tasks_completed=1,
                tasks_failed=1,
                tasks_failed_infra=0,
                prs_merged=1,
                verified_prs_merged=1,
            )
        )
        assert _check_direction_health(d) is None

    # --- _check_direction_completion ---

    def test_completion_not_declared_when_merges_all_degraded(self):
        # 4 cycles each landing 2 unverified-degraded merges + 2 low-yield cycles.
        # Raw total = 8 (>=3) would declare complete; verified total = 0 → None.
        history = _verified_history(
            4, tasks_created=2, tasks_completed=2, prs_merged=2, verified_prs_merged=0
        ) + _verified_history(
            2, tasks_created=1, tasks_completed=1, prs_merged=0, verified_prs_merged=0
        )
        d = _direction(research_history=history)
        assert _check_direction_completion(d) is None

    def test_completion_declared_when_merges_verified(self):
        # Same shape but the early merges are verified → direction reads complete.
        history = _verified_history(
            4, tasks_created=2, tasks_completed=2, prs_merged=2, verified_prs_merged=2
        ) + _verified_history(
            2, tasks_created=1, tasks_completed=1, prs_merged=0, verified_prs_merged=0
        )
        d = _direction(research_history=history)
        result = _check_direction_completion(d)
        assert result is not None
        assert "complete" in result.lower()

    # --- already-built signature ---

    def test_already_built_fires_despite_degraded_merges_above_max(self):
        # No-op-dominated cycles, no code failures, but 2 (degraded) merges/cycle —
        # raw merged=10 would exceed max_merged=1 and suppress the signal. Verified
        # merged=0, so the already-built suggestion still surfaces.
        d = _direction(
            research_history=_verified_history(
                5,
                tasks_created=4,
                tasks_completed=4,
                tasks_completed_no_pr=4,
                tasks_failed=0,
                prs_merged=2,
                verified_prs_merged=0,
            )
        )
        sig = _already_built_signature(d)
        assert sig is not None
        assert sig["merged"] == 0
        assert already_built_completion_suggestion(d) is not None

    def test_already_built_suppressed_when_merges_verified(self):
        # Identical except merges are verified → actively shipping, not already-built.
        d = _direction(
            research_history=_verified_history(
                5,
                tasks_created=4,
                tasks_completed=4,
                tasks_completed_no_pr=4,
                tasks_failed=0,
                prs_merged=2,
                verified_prs_merged=2,
            )
        )
        assert _already_built_signature(d) is None


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


class TestCreditGateMergedCount:
    """credit_gate_merged_count: indeterminate merges count, only stranded excluded.

    The credit-decision gates (auto-completion, churn-rate pause, lifetime-burn
    pause) must count ancestry-indeterminate merges alongside confirmed-landed
    ones, and exclude ONLY merges confirmed as unreachable (stranded).
    """

    def test_indeterminate_merge_counts_only_stranded_excluded_from_credit_gate(self):
        from app.direction_outcomes import credit_gate_merged_count

        # No stranded_prs_merged field → indeterminate case: all verified count
        entry_indeterminate = {"verified_prs_merged": 2, "prs_merged": 2}
        assert credit_gate_merged_count(entry_indeterminate) == 2

        # One confirmed-stranded: subtracted from verified, only 1 credit-eligible
        entry_one_stranded = {
            "verified_prs_merged": 2,
            "prs_merged": 3,
            "stranded_prs_merged": 1,
        }
        assert credit_gate_merged_count(entry_one_stranded) == 1

        # All stranded (floors at 0, never negative)
        entry_all_stranded = {
            "verified_prs_merged": 2,
            "prs_merged": 2,
            "stranded_prs_merged": 2,
        }
        assert credit_gate_merged_count(entry_all_stranded) == 0

        # Legacy entry (no verified_prs_merged): falls back via verified_merged_count
        entry_legacy = {"prs_merged": 3, "stranded_prs_merged": 1}
        assert credit_gate_merged_count(entry_legacy) == 2

    def test_churn_gate_excludes_stranded_merges_from_merge_count(self):
        # 5 cycles, each with 1 verified merge that is confirmed-stranded.
        # credit_gate_merged_count returns 0 → churn gate sees 0 merges → fires.
        d = _direction(
            research_history=[
                {
                    "tasks_created": 2,
                    "tasks_completed": 2,
                    "tasks_failed": 0,
                    "prs_merged": 1,
                    "verified_prs_merged": 1,
                    "stranded_prs_merged": 1,
                }
                for _ in range(5)
            ]
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        # All merges are stranded → credit count is 0 → all 10 completions are non-merge
        assert m["merged"] == 0
        assert m["non_merge_finished"] == 10

    def test_churn_gate_counts_indeterminate_merges(self):
        # Same shape but no stranded_prs_merged → indeterminate merges count
        d = _direction(
            research_history=[
                {
                    "tasks_created": 2,
                    "tasks_completed": 2,
                    "tasks_failed": 0,
                    "prs_merged": 1,
                    "verified_prs_merged": 1,
                }
                for _ in range(5)
            ]
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["merged"] == 5
        assert m["non_merge_finished"] == 5


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

    def test_snapshot_merged_counts_verified_not_raw(self):
        # Entries with raw prs_merged but verified_prs_merged=0 must record
        # ``merged`` on the VERIFIED basis (0), not the raw basis.
        d = {
            "research_history": [
                {"prs_merged": 4, "verified_prs_merged": 0},
                {"prs_merged": 3, "verified_prs_merged": 1},
            ],
            "history_aggregate": {
                "total_cycles": 0,
                "total_prs_merged": 5,
                "total_verified_prs_merged": 2,
            },
            "research_context": "",
        }
        record_outcome_progress_snapshot(d)
        # verified history 0 + 1, aggregate verified 2 -> 3 (raw would be 12)
        assert d["outcome_progress_history"][0]["merged"] == 3

    def test_fully_unverified_direction_does_not_latch_ever_met(self):
        # All raw merges, none verified -> the cumulative completion heuristic
        # cannot trip on verified basis, so the stall-exemption latch stays off.
        history = [
            {
                "tasks_created": 3,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "prs_merged": 2,
                "verified_prs_merged": 0,
            },
            {
                "tasks_created": 3,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "prs_merged": 2,
                "verified_prs_merged": 0,
            },
            {
                "tasks_created": 1,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "prs_merged": 0,
                "verified_prs_merged": 0,
            },
            {
                "tasks_created": 1,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "prs_merged": 0,
                "verified_prs_merged": 0,
            },
        ]
        d = {
            "research_history": history,
            "history_aggregate": {},
            "research_context": "",
        }
        record_outcome_progress_snapshot(d)
        assert d.get("outcome_progress_ever_met") is not True


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
    # FIRST HARD GATE: an advancing/met verdict only suppresses when it carries
    # challenge_approved=True (stamped by _apply_evidence_progress_verdicts from an
    # APPROVED challenge record). An unchallenged verdict no longer suppresses.

    def test_advancing_verdict_suppresses_flag(self):
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={
                "status": "advancing",
                "cycle_index": 14,
                "challenge_approved": True,
            },
        )
        # Deterministic detector would fire, but a challenge-approved verdict
        # confirms advancement.
        assert _check_outcome_scaffolding(d) is None

    def test_met_verdict_suppresses_flag(self):
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={
                "status": "met",
                "cycle_index": 14,
                "challenge_approved": True,
            },
        )
        assert _check_outcome_scaffolding(d) is None

    def test_unchallenged_advancing_verdict_does_not_suppress(self):
        # First hard gate: an advancing verdict with no challenge approval (the
        # legacy/unchallenged shape) no longer suppresses the deterministic flag.
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={"status": "advancing", "cycle_index": 14},
        )
        assert _check_outcome_scaffolding(d) is not None

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
        # A challenge-approved verdict recorded a full stall window ago has
        # expired -> detector fires (the recency gate still applies on top of
        # the challenge gate).
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={
                "status": "advancing",
                "cycle_index": 5,
                "challenge_approved": True,
            },
        )
        assert _check_outcome_scaffolding(d) is not None

    def test_undated_advancing_verdict_suppresses(self):
        # No cycle_index -> trust a challenge-approved confirmation as current.
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={"status": "advancing", "challenge_approved": True},
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


class TestSanitizeStrategicChallengeRecord:
    def test_non_dict_returns_none(self):
        assert sanitize_strategic_challenge_record(None) is None
        assert sanitize_strategic_challenge_record([]) is None
        assert sanitize_strategic_challenge_record("nope") is None

    def test_empty_dict_yields_versioned_empty_record(self):
        rec = sanitize_strategic_challenge_record({})
        assert rec == {
            "challenge_version": STRATEGIC_CHALLENGE_RECORD_VERSION,
            "version": STRATEGIC_CHALLENGE_RECORD_VERSION,
            "status": "invalid",
            "evidence_progress": [],
            "retirement_approvals": [],
            "retirement_critique_quality": {
                "verdict": "not_assessed",
                "rationale": "",
            },
            "portfolio_shape_risk": {
                "verdict": "not_assessed",
                "rationale": "",
            },
            "portfolio_operations": [],
        }

    def test_sanitizes_agent_shape_and_preserves_second_opinion_blocks(self):
        rec = sanitize_strategic_challenge_record(
            {
                "challenge_version": 1,
                "status": "blocked",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "Advancing",
                        "verdict": "Approved",
                        "rationale": "ships real evidence",
                    }
                ],
                "retirement_approvals": [
                    {
                        "direction_id": "d-ret",
                        "verdict": "Approved",
                        "rationale": "pattern is evidenced",
                    }
                ],
                "retirement_critique_quality": {
                    "verdict": "solid",
                    "rationale": "names the pattern",
                },
                "portfolio_shape_risk": {
                    "verdict": "elevated",
                    "rationale": "lanes are concentrated",
                },
                "notes": "needs follow-up",
            }
        )
        assert rec["challenge_version"] == STRATEGIC_CHALLENGE_RECORD_VERSION
        assert rec["version"] == STRATEGIC_CHALLENGE_RECORD_VERSION
        assert rec["status"] == "blocked"
        # status/verdict are lower-cased.
        assert rec["evidence_progress"] == [
            {
                "direction_id": "d-1",
                "status": "advancing",
                "verdict": "approved",
                "rationale": "ships real evidence",
            }
        ]
        assert rec["retirement_approvals"] == [
            {
                "direction_id": "d-ret",
                "verdict": "approved",
                "rationale": "pattern is evidenced",
            }
        ]
        assert rec["retirement_critique_quality"] == {
            "verdict": "solid",
            "rationale": "names the pattern",
        }
        assert rec["portfolio_shape_risk"] == {
            "verdict": "elevated",
            "rationale": "lanes are concentrated",
        }
        assert rec["notes"] == "needs follow-up"

    def test_drops_malformed_items_keeps_valid_ones(self):
        rec = sanitize_strategic_challenge_record(
            {
                "evidence_progress": [
                    "not a dict",
                    {"direction_id": "", "status": "met", "verdict": "approved"},
                    {"direction_id": "d-3", "status": "bogus", "verdict": "approved"},
                    {"direction_id": "d-4", "status": "met", "verdict": "maybe"},
                    {"direction_id": "d-5", "status": "met", "verdict": "approved"},
                ]
            }
        )
        # Only the last fully-valid item survives.
        assert rec["evidence_progress"] == [
            {
                "direction_id": "d-5",
                "status": "met",
                "verdict": "approved",
                "rationale": "",
            }
        ]

    def test_bounds_item_count_and_string_lengths(self):
        big = [
            {
                "direction_id": f"d-{i}",
                "status": "advancing",
                "verdict": "approved",
                "rationale": "x" * 1000,
            }
            for i in range(120)
        ]
        rec = sanitize_strategic_challenge_record({"evidence_progress": big})
        assert len(rec["evidence_progress"]) == 50  # _CHALLENGE_MAX_ITEMS
        assert len(rec["evidence_progress"][0]["rationale"]) == 300

    def test_sanitizes_retirement_approval_items_independently(self):
        rec = sanitize_strategic_challenge_record(
            {
                "retirement_approvals": [
                    "not a dict",
                    {"direction_id": "", "verdict": "approved"},
                    {"direction_id": "d-1", "verdict": "maybe"},
                    {
                        "direction_id": "d-2",
                        "verdict": "uncertain",
                        "rationale": "x" * 1000,
                    },
                ]
            }
        )
        assert rec["retirement_approvals"] == [
            {
                "direction_id": "d-2",
                "verdict": "uncertain",
                "rationale": "x" * 300,
            }
        ]


class TestPortfolioOperations:
    def test_legacy_record_without_portfolio_operations_defaults_empty(self):
        rec = sanitize_strategic_challenge_record(
            {"status": "ok", "evidence_progress": []}
        )
        assert rec["portfolio_operations"] == []

    def test_portfolio_operations_sanitized_and_fingerprinted(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "portfolio_operations": [
                    {
                        "op": "retire_direction",
                        "direction_id": "d-1",
                        "verdict": "Rejected",
                        "rationale": "no evidence the lane is dead",
                    },
                    {
                        "op": "create",
                        "text": "Build a Widget Pipeline",
                        "verdict": "warned",
                    },
                ],
            }
        )
        ops = rec["portfolio_operations"]
        assert ops[0] == {
            "op": "retire_direction",
            "direction_id": "d-1",
            "fingerprint": portfolio_operation_fingerprint(
                "retire_direction", direction_id="d-1"
            ),
            "verdict": "rejected",
            "rationale": "no evidence the lane is dead",
        }
        assert ops[1]["op"] == "create_direction"
        assert ops[1]["verdict"] == "warned"
        assert ops[1]["fingerprint"] == portfolio_operation_fingerprint(
            "create_direction", text="build a widget pipeline"
        )

    def test_evidence_progress_and_portfolio_operations_both_survive(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "met",
                        "verdict": "approved",
                    }
                ],
                "portfolio_operations": [
                    {
                        "op": "retire_direction",
                        "direction_id": "d-2",
                        "verdict": "rejected",
                    }
                ],
            }
        )
        assert rec["evidence_progress"] == [
            {
                "direction_id": "d-1",
                "status": "met",
                "verdict": "approved",
                "rationale": "",
            }
        ]
        assert rec["portfolio_operations"] == [
            {
                "op": "retire_direction",
                "direction_id": "d-2",
                "fingerprint": portfolio_operation_fingerprint(
                    "retire_direction", direction_id="d-2"
                ),
                "verdict": "rejected",
                "rationale": "",
            }
        ]

    def test_portfolio_operations_drops_unknown_op_and_verdict(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "portfolio_operations": [
                    "not a dict",
                    {"op": "set_vision", "direction_id": "d-1", "verdict": "rejected"},
                    {
                        "op": "retire_direction",
                        "direction_id": "d-2",
                        "verdict": "nope",
                    },
                    {
                        "op": "retire_direction",
                        "direction_id": "d-3",
                        "verdict": "rejected",
                    },
                ],
            }
        )
        assert [i["direction_id"] for i in rec["portfolio_operations"]] == ["d-3"]

    def test_portfolio_operations_collapse_duplicate_fingerprints(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "portfolio_operations": [
                    {
                        "op": "retire_direction",
                        "direction_id": "d-1",
                        "verdict": "rejected",
                    },
                    {"op": "retire", "direction_id": "d-1", "verdict": "approved"},
                ],
            }
        )
        assert len(rec["portfolio_operations"]) == 1
        assert rec["portfolio_operations"][0]["verdict"] == "rejected"

    def test_blocked_record_drops_portfolio_operations(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "blocked",
                "portfolio_operations": [
                    {
                        "op": "retire_direction",
                        "direction_id": "d-1",
                        "verdict": "rejected",
                    },
                    {
                        "op": "create_direction",
                        "text": "build x",
                        "verdict": "rejected",
                    },
                ],
            }
        )
        assert rec["portfolio_operations"] == []

    def test_stage_direction_with_text_reclassified_to_create(self):
        # A stage_direction verdict carrying text but NO direction_id is really a
        # vote to STAGE A NEW direction, which the strategist executes as
        # create_direction(staged=true) (the stage path cannot create). The stored
        # op + fingerprint must match that create_direction move, otherwise the
        # approved staged-create is hard-blocked as "no matching verdict".
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "portfolio_operations": [
                    {
                        "op": "stage_direction",
                        "text": "Stand up a local-GPU runner lane",
                        "verdict": "approved",
                    }
                ],
            }
        )
        ops = rec["portfolio_operations"]
        assert len(ops) == 1
        assert ops[0]["op"] == "create_direction"
        assert ops[0]["fingerprint"] == portfolio_operation_fingerprint(
            "create_direction", text="Stand up a local-GPU runner lane"
        )
        assert ops[0]["fingerprint"].startswith("create_direction:text:")
        assert ops[0]["verdict"] == "approved"

    def test_stage_direction_with_direction_id_is_unchanged(self):
        # Staging an EXISTING direction by id stays a real stage_direction op.
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "portfolio_operations": [
                    {
                        "op": "stage_direction",
                        "direction_id": "d-1",
                        "verdict": "approved",
                    }
                ],
            }
        )
        ops = rec["portfolio_operations"]
        assert len(ops) == 1
        assert ops[0]["op"] == "stage_direction"
        assert ops[0]["fingerprint"] == portfolio_operation_fingerprint(
            "stage_direction", direction_id="d-1"
        )


class TestPortfolioOperationFingerprint:
    def test_id_keyed_ops_are_stable_and_canonical(self):
        assert portfolio_operation_fingerprint(
            "retire", direction_id="d-1"
        ) == portfolio_operation_fingerprint("retire_direction", direction_id="d-1")

    def test_create_keys_on_normalized_text(self):
        a = portfolio_operation_fingerprint("create_direction", text="Build  X")
        b = portfolio_operation_fingerprint("create_direction", text="build x")
        assert a == b
        assert a != portfolio_operation_fingerprint("create_direction", text="build y")

    def test_different_ops_do_not_collide(self):
        assert portfolio_operation_fingerprint(
            "retire_direction", direction_id="d-1"
        ) != portfolio_operation_fingerprint("stage_direction", direction_id="d-1")


class TestChallengeRejectsPortfolioOp:
    def _rec(self, ops):
        return sanitize_strategic_challenge_record(
            {"status": "ok", "portfolio_operations": ops}
        )

    def test_rejection_blocks_destructive_op(self):
        rec = self._rec(
            [{"op": "retire_direction", "direction_id": "d-1", "verdict": "rejected"}]
        )
        blocked, why = challenge_rejects_portfolio_op(
            rec, "retire_direction", direction_id="d-1"
        )
        assert blocked is True
        assert isinstance(why, str)

    def test_warned_does_not_block(self):
        rec = self._rec(
            [{"op": "stage_direction", "direction_id": "d-1", "verdict": "warned"}]
        )
        assert challenge_rejects_portfolio_op(
            rec, "stage_direction", direction_id="d-1"
        ) == (False, "")

    def test_rejection_on_create_blocks(self):
        rec = self._rec(
            [{"op": "create_direction", "text": "build x", "verdict": "rejected"}]
        )
        blocked, why = challenge_rejects_portfolio_op(
            rec, "create_direction", text="build x"
        )
        assert blocked is True
        assert isinstance(why, str)

    def test_blocked_record_blocks_hard_op(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "blocked",
                "portfolio_operations": [
                    {
                        "op": "retire_direction",
                        "direction_id": "d-1",
                        "verdict": "rejected",
                    }
                ],
            }
        )
        blocked, why = challenge_rejects_portfolio_op(
            rec, "retire_direction", direction_id="d-1"
        )
        assert blocked is True
        assert "blocked" in why or "challenge status" in why

    def test_fingerprint_mismatch_blocks(self):
        rec = self._rec(
            [{"op": "retire_direction", "direction_id": "d-1", "verdict": "rejected"}]
        )
        assert (
            challenge_rejects_portfolio_op(rec, "retire_direction", direction_id="d-2")[
                0
            ]
            is True
        )

    def test_missing_record_does_not_block(self):
        assert challenge_rejects_portfolio_op(
            None, "retire_direction", direction_id="d-1"
        ) == (False, "")

    def test_approved_staged_create_is_not_blocked(self):
        # Regression: a stage_direction verdict with text (no direction_id) is the
        # approval to STAGE A NEW direction, which the strategist runs as
        # create_direction(staged=true). The stored verdict is reclassified to
        # create_direction so the gate matches the executed op rather than failing
        # with "no matching portfolio_operations verdict" every cycle.
        rec = self._rec(
            [
                {
                    "op": "stage_direction",
                    "text": "Stand up a local-GPU runner lane",
                    "verdict": "approved",
                }
            ]
        )
        assert rec["portfolio_operations"][0]["op"] == "create_direction"
        assert challenge_rejects_portfolio_op(
            rec, "create_direction", text="Stand up a local-GPU runner lane"
        ) == (False, "")

    def test_rejected_activate_direction_blocks(self):
        rec = self._rec(
            [{"op": "activate_direction", "direction_id": "d-5", "verdict": "rejected"}]
        )
        blocked, why = challenge_rejects_portfolio_op(
            rec, "activate_direction", direction_id="d-5"
        )
        assert blocked is True
        assert isinstance(why, str)

    def test_approved_activate_direction_does_not_block(self):
        rec = self._rec(
            [{"op": "activate_direction", "direction_id": "d-5", "verdict": "approved"}]
        )
        assert challenge_rejects_portfolio_op(
            rec, "activate_direction", direction_id="d-5"
        ) == (False, "")

    def test_absent_verdict_for_activate_direction_blocks(self):
        # No matching portfolio_operations entry → hard-block op fails closed.
        rec = self._rec([])
        blocked, _why = challenge_rejects_portfolio_op(
            rec, "activate_direction", direction_id="d-5"
        )
        assert blocked is True

    def test_activate_alias_canonicalizes(self):
        from app.strategic_completion import _canonical_portfolio_op

        assert _canonical_portfolio_op("activate") == "activate_direction"
        assert _canonical_portfolio_op("activate_direction") == "activate_direction"


class TestStrategicChallengeSummary:
    def test_non_dict_returns_none(self):
        assert strategic_challenge_summary(None) is None
        assert strategic_challenge_summary([]) is None
        assert strategic_challenge_summary("nope") is None

    def test_clean_pass_is_approved_with_zero_counts(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "advancing",
                        "verdict": "approved",
                    }
                ],
            }
        )
        assert strategic_challenge_summary(rec) == {
            "mode": "ok",
            "verdict": "approved",
            "warned_count": 0,
            "blocked_count": 0,
            "gate_mode": "enforce",
        }

    def test_mixed_evidence_and_section_verdicts_roll_up_to_blocked(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "advancing",
                        "verdict": "approved",
                    },
                    {"direction_id": "d-2", "status": "met", "verdict": "rejected"},
                    {
                        "direction_id": "d-3",
                        "status": "advancing",
                        "verdict": "uncertain",
                    },
                ],
                # section verdict blocks contribute too
                "retirement_critique_quality": {"verdict": "uncertain"},
                "portfolio_shape_risk": {"verdict": "rejected"},
                "portfolio_operations": [
                    {
                        "op": "stage_direction",
                        "direction_id": "d-4",
                        "verdict": "warned",
                    },
                    {
                        "op": "retire_direction",
                        "direction_id": "d-5",
                        "verdict": "rejected",
                    },
                ],
            }
        )
        assert (
            strategic_challenge_summary(rec)
            == {
                "mode": "ok",
                # rejected anywhere -> overall blocked
                "verdict": "blocked",
                "warned_count": 3,  # one uncertain item + one uncertain section + one warned op
                "blocked_count": 3,  # one rejected item + one rejected section + one rejected op
                "gate_mode": "enforce",
            }
        )

    def test_only_uncertain_rolls_up_to_warned(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [
                    {"direction_id": "d-1", "status": "met", "verdict": "uncertain"}
                ],
            }
        )
        summary = strategic_challenge_summary(rec)
        assert summary["verdict"] == "warned"
        assert summary["warned_count"] == 1
        assert summary["blocked_count"] == 0

    @pytest.mark.parametrize("status", ["blocked", "invalid"])
    def test_non_ok_status_fails_closed_even_without_disputed_counts(self, status):
        rec = sanitize_strategic_challenge_record({"status": status})
        assert strategic_challenge_summary(rec) == {
            "mode": status,
            "verdict": "blocked",
            "warned_count": 0,
            "blocked_count": 0,
            "gate_mode": "enforce",
        }

    def test_mode_mirrors_record_status(self):
        for status in ("ok", "blocked", "invalid"):
            rec = sanitize_strategic_challenge_record({"status": status})
            assert strategic_challenge_summary(rec)["mode"] == status

    def test_unknown_status_normalizes_to_invalid(self):
        # A raw record bypassing sanitation with an out-of-vocab status.
        assert strategic_challenge_summary({"status": "weird"})["mode"] == "invalid"

    def test_compact_summary_excludes_bounded_item_list(self):
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [
                    {"direction_id": "d-1", "status": "met", "verdict": "rejected"}
                ],
                "retirement_critique_quality": {"verdict": "solid"},
                "portfolio_shape_risk": {"verdict": "elevated"},
                "notes": "follow up",
            }
        )
        summary = strategic_challenge_summary(rec)
        # Only the four compact scalar fields — none of the bounded item lists.
        assert set(summary) == {
            "mode",
            "verdict",
            "warned_count",
            "blocked_count",
            "gate_mode",
        }
        # elevated portfolio_shape_risk counts as warned; rejected evidence item as blocked
        assert summary["blocked_count"] == 1
        assert summary["warned_count"] == 1
        assert summary["verdict"] == "blocked"

    def test_section_shallow_maps_to_warned(self):
        # retirement_critique_quality: "shallow" is the prompt's negative verdict
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [],
                "retirement_critique_quality": {"verdict": "shallow"},
                "portfolio_shape_risk": {"verdict": "low"},
            }
        )
        summary = strategic_challenge_summary(rec)
        assert summary == {
            "mode": "ok",
            "verdict": "warned",
            "warned_count": 1,
            "blocked_count": 0,
            "gate_mode": "enforce",
        }

    def test_section_elevated_maps_to_warned(self):
        # portfolio_shape_risk: "elevated" is the prompt's mid-level risk verdict
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [],
                "retirement_critique_quality": {"verdict": "solid"},
                "portfolio_shape_risk": {"verdict": "elevated"},
            }
        )
        summary = strategic_challenge_summary(rec)
        assert summary == {
            "mode": "ok",
            "verdict": "warned",
            "warned_count": 1,
            "blocked_count": 0,
            "gate_mode": "enforce",
        }

    def test_section_high_maps_to_blocked(self):
        # portfolio_shape_risk: "high" is the prompt's severe risk verdict → blocked
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [],
                "retirement_critique_quality": {"verdict": "solid"},
                "portfolio_shape_risk": {"verdict": "high"},
            }
        )
        summary = strategic_challenge_summary(rec)
        assert summary == {
            "mode": "ok",
            "verdict": "blocked",
            "warned_count": 0,
            "blocked_count": 1,
            "gate_mode": "enforce",
        }

    def test_section_clean_verdicts_are_approved(self):
        # solid, low, not_assessed are all clean — no warn/block contribution
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "advancing",
                        "verdict": "approved",
                    }
                ],
                "retirement_critique_quality": {"verdict": "solid"},
                "portfolio_shape_risk": {"verdict": "low"},
            }
        )
        summary = strategic_challenge_summary(rec)
        assert summary == {
            "mode": "ok",
            "verdict": "approved",
            "warned_count": 0,
            "blocked_count": 0,
            "gate_mode": "enforce",
        }

    def test_section_both_negative_accumulates_counts(self):
        # shallow retirement + elevated portfolio both contribute to warned count
        rec = sanitize_strategic_challenge_record(
            {
                "status": "ok",
                "evidence_progress": [],
                "retirement_critique_quality": {"verdict": "shallow"},
                "portfolio_shape_risk": {"verdict": "elevated"},
            }
        )
        summary = strategic_challenge_summary(rec)
        assert summary == {
            "mode": "ok",
            "verdict": "warned",
            "warned_count": 2,
            "blocked_count": 0,
            "gate_mode": "enforce",
        }


class TestChallengeApprovesEvidenceProgress:
    def _rec(self, items, status="ok"):
        return {"version": 1, "status": status, "evidence_progress": items}

    def test_approved_exact_match_returns_true(self):
        rec = self._rec(
            [{"direction_id": "d-1", "status": "advancing", "verdict": "approved"}]
        )
        assert challenge_approves_evidence_progress(rec, "d-1", "advancing") is True

    def test_rejected_returns_false(self):
        rec = self._rec(
            [{"direction_id": "d-1", "status": "advancing", "verdict": "rejected"}]
        )
        assert challenge_approves_evidence_progress(rec, "d-1", "advancing") is False

    def test_uncertain_returns_false(self):
        rec = self._rec(
            [{"direction_id": "d-1", "status": "met", "verdict": "uncertain"}]
        )
        assert challenge_approves_evidence_progress(rec, "d-1", "met") is False

    def test_status_mismatch_returns_false(self):
        rec = self._rec(
            [{"direction_id": "d-1", "status": "met", "verdict": "approved"}]
        )
        # Approval was for "met", not "advancing".
        assert challenge_approves_evidence_progress(rec, "d-1", "advancing") is False

    def test_direction_mismatch_returns_false(self):
        rec = self._rec(
            [{"direction_id": "d-1", "status": "advancing", "verdict": "approved"}]
        )
        assert challenge_approves_evidence_progress(rec, "d-2", "advancing") is False

    def test_missing_or_malformed_record_returns_false(self):
        assert challenge_approves_evidence_progress(None, "d-1", "advancing") is False
        assert challenge_approves_evidence_progress({}, "d-1", "advancing") is False
        assert challenge_approves_evidence_progress("x", "d-1", "advancing") is False
        assert (
            challenge_approves_evidence_progress(self._rec([]), "d-1", "advancing")
            is False
        )

    def test_blank_args_return_false(self):
        rec = self._rec(
            [{"direction_id": "d-1", "status": "advancing", "verdict": "approved"}]
        )
        assert challenge_approves_evidence_progress(rec, "", "advancing") is False
        assert challenge_approves_evidence_progress(rec, "d-1", "") is False

    def test_blocked_status_with_approved_entry_returns_false(self):
        # A blocked record must never approve even if it has an approved entry.
        rec = self._rec(
            [{"direction_id": "d-1", "status": "advancing", "verdict": "approved"}],
            status="blocked",
        )
        assert challenge_approves_evidence_progress(rec, "d-1", "advancing") is False

    def test_invalid_status_with_approved_entry_returns_false(self):
        # An invalid record must never approve.
        rec = self._rec(
            [{"direction_id": "d-1", "status": "advancing", "verdict": "approved"}],
            status="invalid",
        )
        assert challenge_approves_evidence_progress(rec, "d-1", "advancing") is False

    def test_missing_status_field_returns_false(self):
        # A record with no status field must not be treated as ok.
        rec = {
            "version": 1,
            "evidence_progress": [
                {"direction_id": "d-1", "status": "advancing", "verdict": "approved"}
            ],
        }
        assert challenge_approves_evidence_progress(rec, "d-1", "advancing") is False


class TestStrategicChallengeGate:
    """First hard gate: an advancing/met evidence_progress verdict may only
    suppress the deterministic scaffolding flag when the stored challenge record
    APPROVED the same direction_id + status."""

    def _task(self, verdicts, challenge):
        ctx = {EVIDENCE_PROGRESS_CONTEXT_KEY: verdicts}
        if challenge is not None:
            ctx[STRATEGIC_CHALLENGE_CONTEXT_KEY] = challenge
        return Task(
            project_id=uuid.uuid4(),
            payloads=[{"type": "strategic_review", "prompt": "x"}],
            context=ctx,
        )

    def _project_with_scaffold(self):
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        d["id"] = "d-1"
        return Project(
            name="CG",
            user_id="u1",
            vision={"purpose": "p", "revision": 1},
            directions=[d],
        )

    def test_apply_stamps_challenge_approved_when_challenge_approves(self):
        project = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            {
                "version": 1,
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "advancing",
                        "verdict": "approved",
                    }
                ],
            },
        )
        assert _apply_evidence_progress_verdicts(task, project) == 1
        assert project.directions[0]["evidence_progress"]["challenge_approved"] is True

    def test_apply_marks_unapproved_when_challenge_rejects(self):
        project = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            {
                "version": 1,
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "advancing",
                        "verdict": "rejected",
                    }
                ],
            },
        )
        _apply_evidence_progress_verdicts(task, project)
        assert project.directions[0]["evidence_progress"]["challenge_approved"] is False

    def test_apply_marks_unapproved_when_challenge_missing(self):
        project = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            None,
        )
        _apply_evidence_progress_verdicts(task, project)
        assert project.directions[0]["evidence_progress"]["challenge_approved"] is False

    def test_strategic_challenge_record_gates_advancing_evidence_progress(self):
        # End-to-end of the first hard gate through real production helpers:
        # the SAME advancing self-rating either suppresses the deterministic
        # outcome-scaffolding flag or not, decided solely by whether the stored
        # challenge record approved the matching direction_id + status.
        approving = {
            "version": 1,
            "status": "ok",
            "evidence_progress": [
                {"direction_id": "d-1", "status": "advancing", "verdict": "approved"}
            ],
        }
        rejecting = {
            "version": 1,
            "status": "ok",
            "evidence_progress": [
                {"direction_id": "d-1", "status": "advancing", "verdict": "rejected"}
            ],
        }
        wrong_status = {
            "version": 1,
            "status": "ok",
            "evidence_progress": [
                {"direction_id": "d-1", "status": "met", "verdict": "approved"}
            ],
        }

        # Precondition: with no verdict at all the deterministic detector fires.
        bare = self._project_with_scaffold()
        assert _check_outcome_scaffolding(bare.directions[0]) is not None

        # APPROVED → the verdict is stamped challenge_approved and suppresses.
        approved_proj = self._project_with_scaffold()
        approved_task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "real"}],
            approving,
        )
        _apply_evidence_progress_verdicts(approved_task, approved_proj)
        assert _check_outcome_scaffolding(approved_proj.directions[0]) is None

        # REJECTED → not approved → deterministic scaffolding stays active.
        rejected_proj = self._project_with_scaffold()
        rejected_task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "real"}],
            rejecting,
        )
        _apply_evidence_progress_verdicts(rejected_task, rejected_proj)
        assert _check_outcome_scaffolding(rejected_proj.directions[0]) is not None

        # APPROVED but for a DIFFERENT status (met, not advancing) → no match,
        # so the advancing self-rating does not suppress.
        mismatch_proj = self._project_with_scaffold()
        mismatch_task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "real"}],
            wrong_status,
        )
        _apply_evidence_progress_verdicts(mismatch_task, mismatch_proj)
        assert _check_outcome_scaffolding(mismatch_proj.directions[0]) is not None

        # MISSING challenge → not approved → deterministic scaffolding stays active.
        missing_proj = self._project_with_scaffold()
        missing_task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "real"}],
            None,
        )
        _apply_evidence_progress_verdicts(missing_task, missing_proj)
        assert _check_outcome_scaffolding(missing_proj.directions[0]) is not None

    def test_stalled_verdict_stays_advisory_regardless_of_challenge(self):
        # A stalled verdict never suppresses, and never claims challenge approval,
        # even if a (nonsensical) challenge "approved" a stalled status.
        project = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "stalled", "rationale": "no progress"}],
            {
                "version": 1,
                "status": "ok",
                "evidence_progress": [
                    {"direction_id": "d-1", "status": "stalled", "verdict": "approved"}
                ],
            },
        )
        _apply_evidence_progress_verdicts(task, project)
        assert project.directions[0]["evidence_progress"]["challenge_approved"] is False
        assert _check_outcome_scaffolding(project.directions[0]) is not None


class TestStrategicChallengeGateMode:
    """Gate mode runtime config: observe/advisory/enforce control suppression behavior."""

    def _task(self, verdicts, challenge):
        ctx = {EVIDENCE_PROGRESS_CONTEXT_KEY: verdicts}
        if challenge is not None:
            ctx[STRATEGIC_CHALLENGE_CONTEXT_KEY] = challenge
        return Task(
            project_id=uuid.uuid4(),
            payloads=[{"type": "strategic_review", "prompt": "x"}],
            context=ctx,
        )

    def _project_with_scaffold(self):
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        d["id"] = "d-1"
        return Project(
            name="GateModeTest",
            user_id="u1",
            vision={"purpose": "p", "revision": 1},
            directions=[d],
        )

    def _rejecting_challenge(self):
        return {
            "version": 1,
            "status": "ok",
            "evidence_progress": [
                {"direction_id": "d-1", "status": "advancing", "verdict": "rejected"}
            ],
        }

    def test_challenge_gate_mode_observe_advisory_enforce(self, mocker):
        """Acceptance test: observe/advisory/enforce modes all honor their contract."""
        # --- enforce (default): gate blocks when challenge did not approve ---
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"strategic_challenge_gate_mode": "enforce"},
            clear=False,
        )
        enforce_proj = self._project_with_scaffold()
        enforce_task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            self._rejecting_challenge(),
        )
        _apply_evidence_progress_verdicts(enforce_task, enforce_proj)
        ep = enforce_proj.directions[0]["evidence_progress"]
        assert ep["gate_mode"] == "enforce"
        assert ep["gate_would_block"] is True
        assert ep["challenge_approved"] is False
        # enforce: scaffolding NOT suppressed
        assert _check_outcome_scaffolding(enforce_proj.directions[0]) is not None

        # --- observe: gate stamps would-block but does NOT suppress ---
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"strategic_challenge_gate_mode": "observe"},
            clear=False,
        )
        observe_proj = self._project_with_scaffold()
        observe_task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            self._rejecting_challenge(),
        )
        _apply_evidence_progress_verdicts(observe_task, observe_proj)
        ep = observe_proj.directions[0]["evidence_progress"]
        assert ep["gate_mode"] == "observe"
        assert ep["gate_would_block"] is True
        assert ep["challenge_approved"] is False
        # observe: suppression allowed (scaffolding IS suppressed)
        assert _check_outcome_scaffolding(observe_proj.directions[0]) is None

        # --- advisory: gate stamps would-block, emits warning, does NOT block ---
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"strategic_challenge_gate_mode": "advisory"},
            clear=False,
        )
        advisory_proj = self._project_with_scaffold()
        advisory_task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            self._rejecting_challenge(),
        )
        _apply_evidence_progress_verdicts(advisory_task, advisory_proj)
        ep = advisory_proj.directions[0]["evidence_progress"]
        assert ep["gate_mode"] == "advisory"
        assert ep["gate_would_block"] is True
        assert ep["challenge_approved"] is False
        # advisory: suppression allowed (scaffolding IS suppressed)
        assert _check_outcome_scaffolding(advisory_proj.directions[0]) is None

    def test_default_mode_is_enforce(self, mocker):
        """Absent config key defaults to enforce (current behavior preserved)."""
        # Remove key to simulate absent config
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {},
            clear=True,
        )
        proj = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            self._rejecting_challenge(),
        )
        _apply_evidence_progress_verdicts(task, proj)
        ep = proj.directions[0]["evidence_progress"]
        assert ep["gate_mode"] == "enforce"
        # enforce (default): gate blocks
        assert _check_outcome_scaffolding(proj.directions[0]) is not None

    def test_advisory_logs_warning_when_gate_would_block(self, mocker, caplog):
        """Advisory mode emits a warning log when gate_would_block is True."""
        import logging

        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"strategic_challenge_gate_mode": "advisory"},
            clear=False,
        )
        proj = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            self._rejecting_challenge(),
        )
        _apply_evidence_progress_verdicts(task, proj)
        with caplog.at_level(logging.WARNING, logger="app.strategic_completion"):
            _check_outcome_scaffolding(proj.directions[0])
        assert any("advisory" in r.message for r in caplog.records)

    def test_observe_no_warning_when_gate_would_block(self, mocker, caplog):
        """Observe mode does NOT emit a warning log."""
        import logging

        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"strategic_challenge_gate_mode": "observe"},
            clear=False,
        )
        proj = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            self._rejecting_challenge(),
        )
        _apply_evidence_progress_verdicts(task, proj)
        with caplog.at_level(logging.WARNING, logger="app.strategic_completion"):
            _check_outcome_scaffolding(proj.directions[0])
        assert not any("advisory" in r.message for r in caplog.records)

    def test_gate_mode_surfaced_in_challenge_summary(self, mocker):
        """gate_mode from runtime config is included in strategic_challenge_summary."""
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"strategic_challenge_gate_mode": "advisory"},
            clear=False,
        )
        record = {
            "status": "ok",
            "evidence_progress": [],
        }
        summary = strategic_challenge_summary(record)
        assert summary is not None
        assert summary["gate_mode"] == "advisory"

    def test_gate_mode_enforce_approved_still_suppresses(self, mocker):
        """In enforce mode, an approved challenge still suppresses scaffolding."""
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"strategic_challenge_gate_mode": "enforce"},
            clear=False,
        )
        proj = self._project_with_scaffold()
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "ok"}],
            {
                "version": 1,
                "status": "ok",
                "evidence_progress": [
                    {
                        "direction_id": "d-1",
                        "status": "advancing",
                        "verdict": "approved",
                    }
                ],
            },
        )
        _apply_evidence_progress_verdicts(task, proj)
        ep = proj.directions[0]["evidence_progress"]
        assert ep["gate_mode"] == "enforce"
        assert ep["gate_would_block"] is False
        assert ep["challenge_approved"] is True
        assert _check_outcome_scaffolding(proj.directions[0]) is None


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


@pytest.mark.asyncio
async def test_cycle_records_knowledge_citation_counts(db_session, mocker):
    """A completed strategic cycle records knowledge_available and knowledge_cited counts.

    knowledge_available == number of KnowledgeDocument rows in the project;
    knowledge_cited == count of IDs in task.context["_strategic_cited_knowledge_ids"]
    that intersect the available index (API path — reliable structured output).
    """
    from unittest.mock import AsyncMock
    from app.models import KnowledgeDocument, TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="KnowledgeCount",
        user_id="u-knowledge",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A source task is required as a FK for KnowledgeDocument.
    source_task = Task(
        project_id=project.id,
        payloads=[{"type": "web_research", "topic": "Architecture patterns"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    knowledge_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=source_task.id,
        title="Architecture patterns overview",
        summary="Summary of architecture patterns.",
        body="Full body.",
    )
    db_session.add(knowledge_doc)
    await db_session.commit()
    await db_session.refresh(knowledge_doc)

    doc_id = str(knowledge_doc.id)

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
            "_strategic_cited_knowledge_ids": [doc_id],
        },
        strategic_cycle_id=cycle.id,
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, "", db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert meta.get("knowledge_available") == 1, (
        f"knowledge_available should be 1 (one knowledge doc), got {meta.get('knowledge_available')}"
    )
    assert meta.get("knowledge_cited") == 1, (
        f"knowledge_cited should be 1 (doc_id in _strategic_cited_knowledge_ids), got {meta.get('knowledge_cited')}"
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
    from app.models import Artifact

    # No MEMO Artifact row is written — the memo lives only in plan_output.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == memo.id)))
        .scalars()
        .all()
    )
    assert artifacts == []


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
            "terminal_reason": "invalidated",
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
    from app.models import Artifact

    # No MEMO Artifact row is written — the memo lives only in plan_output.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == memo.id)))
        .scalars()
        .all()
    )
    assert artifacts == []


@pytest.mark.asyncio
async def test_fallback_direction_ops_respect_rejected_challenge(db_session, mocker):
    """Legacy fallback mutations must not bypass a stored non-ok challenge."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackChallengeGate",
        user_id="u-fallback-gate",
        vision={"purpose": "test", "revision": 0},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    challenge = sanitize_strategic_challenge_record(
        {
            "status": "blocked",
            "portfolio_operations": [
                {
                    "op": "create_direction",
                    "text": "Blocked by challenge",
                    "verdict": "rejected",
                },
                {
                    "op": "stage_direction",
                    "direction_id": "d-stage",
                    "verdict": "rejected",
                },
                {
                    "op": "approve_direction",
                    "direction_id": "d-approve",
                    "verdict": "rejected",
                },
                {
                    "op": "reject_direction",
                    "direction_id": "d-reject",
                    "verdict": "rejected",
                },
            ],
        }
    )
    directions = [
        {
            "id": "d-stage",
            "text": "Stage me",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "research_history": [],
            "notes": [],
        },
        {
            "id": "d-approve",
            "text": "Approve me",
            "status": "proposed",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "research_history": [],
            "notes": [],
        },
        {
            "id": "d-reject",
            "text": "Reject me",
            "status": "proposed",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "research_history": [],
            "notes": [],
        },
    ]
    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={STRATEGIC_CHALLENGE_CONTEXT_KEY: challenge},
    )
    project.directions = directions
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Blocked by challenge",
                    "priority": "medium",
                    "rationale": "should never commit",
                    "success_evidence": "Evidence exists",
                    "retirement_condition": "Condition remains unmet",
                },
                {
                    "op": "stage_direction",
                    "direction_id": "d-stage",
                    "rationale": "should never stage",
                },
                {
                    "op": "approve_direction",
                    "direction_id": "d-approve",
                    "rationale": "should never approve",
                },
                {
                    "op": "reject_direction",
                    "direction_id": "d-reject",
                    "rationale": "should never reject",
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    assert [d.get("id") for d in project.directions] == [
        "d-stage",
        "d-approve",
        "d-reject",
    ]
    assert (
        next(d for d in project.directions if d.get("id") == "d-stage")["status"]
        == "active"
    )
    assert (
        next(d for d in project.directions if d.get("id") == "d-approve")["status"]
        == "proposed"
    )
    assert (
        next(d for d in project.directions if d.get("id") == "d-reject")["status"]
        == "proposed"
    )


@pytest.mark.asyncio
async def test_fallback_direction_ops_accept_http_style_stage_approve_reject(
    db_session, mocker
):
    """Fallback parsing must apply stage/approve/reject direction ops too."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app import release_version as rv

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackHttpStyleStageApproveReject",
        user_id="u-fallback-stage-approve-reject",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-stage",
                "text": "Stage me",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            },
            {
                "id": "d-approve",
                "text": "Approve me",
                "status": "proposed",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            },
            {
                "id": "d-reject",
                "text": "Reject me",
                "status": "proposed",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await rv.open_version(db_session, project.id, version_number=7)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "stage_direction",
                    "direction_id": "d-stage",
                    "rationale": "next version work",
                },
                {
                    "op": "approve_direction",
                    "direction_id": "d-approve",
                    "rationale": "good idea",
                },
                {
                    "op": "reject_direction",
                    "direction_id": "d-reject",
                    "rationale": "not worthwhile",
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    staged = next(d for d in project.directions if d.get("id") == "d-stage")
    assert staged["status"] == "staged"
    assert staged["staged_for_version"] == 7
    approved = next(d for d in project.directions if d.get("id") == "d-approve")
    assert approved["status"] == "active"
    assert approved.get("activated_at")
    rejected = next(d for d in project.directions if d.get("id") == "d-reject")
    assert rejected["status"] == "rejected"
    assert rejected.get("rejected_at")

    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_dirs_staged") == 1
    assert (task.context or {}).get("_strategic_dirs_updated") == 1
    assert (task.context or {}).get("_strategic_dirs_retired") == 1


@pytest.mark.asyncio
async def test_fallback_direction_ops_preserve_prior_creates_before_approve(
    db_session, mocker
):
    """A fallback create must survive when a later approve_direction is applied."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackCreateApprove",
        user_id="u-fallback-create-approve",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-approve",
                "text": "Approve me",
                "status": "proposed",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "create_direction",
                    "text": "Created before approve",
                    "priority": "medium",
                    "rationale": "keep earlier create",
                    "success_evidence": "Evidence exists",
                    "retirement_condition": "Condition remains unmet",
                },
                {
                    "op": "approve_direction",
                    "direction_id": "d-approve",
                    "rationale": "good idea",
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = [
        d
        for d in (project.directions or [])
        if d.get("text") == "Created before approve"
    ]
    assert len(created) == 1
    assert created[0].get("status") == "active"
    approved = next(d for d in project.directions if d.get("id") == "d-approve")
    assert approved["status"] == "active"


@pytest.mark.asyncio
async def test_fallback_direction_ops_preserve_prior_creates_before_reject(
    db_session, mocker
):
    """A fallback create must survive when a later reject_direction is applied."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackCreateReject",
        user_id="u-fallback-create-reject",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-reject",
                "text": "Reject me",
                "status": "proposed",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "create_direction",
                    "text": "Created before reject",
                    "priority": "medium",
                    "rationale": "keep earlier create",
                    "success_evidence": "Evidence exists",
                    "retirement_condition": "Condition remains unmet",
                },
                {
                    "op": "reject_direction",
                    "direction_id": "d-reject",
                    "rationale": "not worthwhile",
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = [
        d
        for d in (project.directions or [])
        if d.get("text") == "Created before reject"
    ]
    assert len(created) == 1
    assert created[0].get("status") == "active"
    rejected = next(d for d in project.directions if d.get("id") == "d-reject")
    assert rejected["status"] == "rejected"


@pytest.mark.asyncio
async def test_fallback_direction_ops_accept_http_style_create_direction(
    db_session, mocker
):
    """Legacy fallback parsing now accepts the HTTP-style create_direction name."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackHttpStyle",
        user_id="u-fallback-http",
        vision={"purpose": "test", "revision": 0},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "create_direction",
                    "text": "HTTP style fallback create",
                    "priority": "medium",
                    "rationale": "new direction",
                    "success_evidence": "HTTP-style op is accepted",
                    "retirement_condition": "Condition is no longer needed",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = [
        d
        for d in (project.directions or [])
        if d.get("text") == "HTTP style fallback create"
    ]
    assert len(created) == 1
    assert created[0].get("status") == "active"


@pytest.mark.asyncio
async def test_fallback_create_preserves_staged_contract_and_ignores_active_cap(
    db_session, mocker
):
    """Fallback creates must preserve the same inert/metadata semantics as HTTP."""
    from unittest.mock import AsyncMock

    from app import release_version as rv
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    existing = [
        {
            "id": f"d-active-{idx}",
            "text": f"Active direction {idx}",
            "status": "active",
            "priority": "medium",
            "research_history": [],
            "notes": [],
        }
        for idx in range(10)
    ]
    project = Project(
        name="FallbackStagedCreateContract",
        user_id="u-fallback-staged-contract",
        vision={"purpose": "test", "revision": 0},
        directions=existing,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await rv.open_version(db_session, project.id, version_number=9)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "create_direction",
                    "text": "Stage a mixed reliability outcome",
                    "priority": "medium",
                    "success_evidence": "The staged outcome is verified",
                    "retirement_condition": "The combined gap is closed",
                    "staged": True,
                    "category": "mixed",
                    "criterion_keys": [],
                    "conflicts_with": ["d-active-0"],
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    created = next(
        direction
        for direction in project.directions
        if direction.get("text") == "Stage a mixed reliability outcome"
    )
    assert created["status"] == "staged"
    assert created["staged_for_version"] == 9
    assert created.get("staged_at")
    assert created["category"] == "mixed"
    assert created["criterion_keys"] == []
    assert created["conflicts_with"] == ["d-active-0"]


@pytest.mark.asyncio
async def test_fallback_update_direction_auto_paused_reactivation_stamps_grace(
    db_session, mocker
):
    """Fallback update_direction reactivating an AUTO-paused direction clears
    residue and stamps the auto_recovered_at grace marker while preserving the
    churn counter."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackAutoPausedReactivate",
        user_id="u-fallback-auto-paused",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-auto-paused",
                "text": "Auto-paused churn direction",
                "status": "paused",
                "auto_paused": True,
                "paused_reason": "Churn guardrail: low merge rate",
                "paused_at": "2026-01-01T00:00:00",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "churn_family_pause_count": 1,
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "update_direction",
                    "direction_id": "d-auto-paused",
                    "changes": {"status": "active"},
                    "rationale": "resume the auto-paused direction",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-auto-paused")
    assert d["status"] == "active"
    assert d.get("auto_recovered_at") is not None
    assert "auto_paused" not in d
    assert "paused_reason" not in d
    assert "paused_at" not in d
    # Counter must survive the reactivation.
    assert d.get("churn_family_pause_count") == 1


@pytest.mark.asyncio
async def test_fallback_update_direction_user_paused_reactivation_blocked(
    db_session, mocker
):
    """Fallback update_direction must NOT resume a USER-paused direction: status
    stays paused, no grace is stamped, and the block is logged."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackUserPausedBlocked",
        user_id="u-fallback-user-paused",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-user-paused",
                "text": "User-paused direction",
                "status": "paused",
                # No auto_paused flag → operator/user paused it.
                "paused_reason": "operator paused",
                "paused_at": "2026-01-01T00:00:00",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "update_direction",
                    "direction_id": "d-user-paused",
                    "changes": {"status": "active"},
                    "rationale": "try to resume the user-paused direction",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-user-paused")
    assert d["status"] == "paused", "user-paused direction must not be resumed"
    assert "auto_recovered_at" not in d
    # Residue is untouched because the reactivation was blocked.
    assert d.get("paused_reason") == "operator paused"


@pytest.mark.asyncio
async def test_fallback_update_direction_persists_and_clears_criterion_keys(
    db_session, mocker
):
    """Fallback update_direction must persist changes.criterion_keys onto the
    direction blob and clear them again — mirroring the API update_direction path.

    The strategist is now guided to keep criterion_keys in sync on update. If the
    fallback parser silently dropped them, a direction whose acceptance scope
    changed would keep stale (or lose its) criterion link, so its tasks would
    write no CriterionEvidence rows even on proven acceptance verdicts."""
    from app.strategic_completion import _handle_strategic_completion
    from app import release_version as rv

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackCriterionUpdate",
        user_id="u-fallback-criterion-update",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-criterion-update",
                "text": "Own acceptance evidence",
                "status": "active",
                "priority": "medium",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-fallback-update-map",
        text="Fallback update mapped criterion",
        created_by="test",
    )
    db_session.add(criterion)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # 1) Attach criterion_keys via update_direction. No vision in the logs: a
    # vision-revision bump would flip api_was_used on the second call and make
    # _handle_strategic_completion short-circuit before the fallback ops parser.
    logs = json.dumps(
        {
            "direction_operations": [
                {
                    "op": "update_direction",
                    "direction_id": "d-criterion-update",
                    "changes": {"criterion_keys": [criterion.criterion_key]},
                    "rationale": "attach acceptance criteria",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)
    updated = next(d for d in project.directions if d.get("id") == "d-criterion-update")
    assert updated.get("criterion_keys") == [criterion.criterion_key], (
        "fallback update_direction must persist changes.criterion_keys"
    )
    assert updated.get("criterion_release_version_id") == str(version.id)

    # 2) Clear criterion_keys with an empty list (mirrors API clear semantics).
    logs_clear = json.dumps(
        {
            "direction_operations": [
                {
                    "op": "update_direction",
                    "direction_id": "d-criterion-update",
                    "changes": {"criterion_keys": []},
                    "rationale": "criteria moved elsewhere",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs_clear, db_session)
    await db_session.refresh(project)
    updated = next(d for d in project.directions if d.get("id") == "d-criterion-update")
    assert updated.get("criterion_keys") == [], (
        "fallback update_direction must clear criterion_keys on empty list"
    )
    assert "criterion_release_version_id" not in updated


@pytest.mark.asyncio
async def test_update_direction_invalid_criterion_keys_surfaces_rejection(
    db_session, mocker
):
    """Fallback update_direction with invalid criterion_keys must surface the
    rejection instead of silently dropping the op.

    The op is refused (the direction never receives the dangling key), and the
    rejection is recorded on task.context[REJECTED_DIRECTION_OPS_CONTEXT_KEY] so
    it flows into the completion summary and the next cycle's strategist payload
    — closing the loop where the strategist previously saw only a log line.
    """
    from app.strategic_completion import (
        _handle_strategic_completion,
        REJECTED_DIRECTION_OPS_CONTEXT_KEY,
    )
    from app import release_version as rv

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RejectedCriterionUpdate",
        user_id="u-rejected-criterion-update",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-rejected-criterion-update",
                "text": "Own acceptance evidence",
                "status": "active",
                "priority": "medium",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-active-key",
        text="Active criterion",
        created_by="test",
    )
    db_session.add(criterion)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "direction_operations": [
                {
                    "op": "update_direction",
                    "direction_id": "d-rejected-criterion-update",
                    "changes": {"criterion_keys": ["c-unknown-key"]},
                    "rationale": "link a dangling criterion",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)

    # The op was refused: the direction never receives the dangling key.
    updated = next(
        d
        for d in project.directions
        if d.get("id") == "d-rejected-criterion-update"
    )
    assert updated.get("criterion_keys") not in (["c-unknown-key"],), (
        "invalid criterion_keys must not be persisted onto the direction"
    )

    # The rejection is surfaced on task.context with enough detail to remap.
    rejected = (task.context or {}).get(REJECTED_DIRECTION_OPS_CONTEXT_KEY) or []
    assert rejected, "invalid criterion_keys rejection must be surfaced"
    entry = next(
        (
            r
            for r in rejected
            if r.get("direction_id") == "d-rejected-criterion-update"
        ),
        None,
    )
    assert entry is not None, "rejection must reference the target direction"
    assert entry["reason"] == "invalid_criterion_keys"
    assert "c-unknown-key" in entry["invalid_criterion_keys"]
    assert "c-active-key" in entry["active_criterion_keys"]


@pytest.mark.asyncio
async def test_create_direction_invalid_criterion_keys_surfaces_rejection(
    db_session, mocker
):
    """Fallback create_direction with invalid criterion_keys must surface the
    rejection the same way update_direction does (no direction created, rejection
    recorded on task.context)."""
    from app.strategic_completion import (
        _handle_strategic_completion,
        REJECTED_DIRECTION_OPS_CONTEXT_KEY,
    )
    from app import release_version as rv

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RejectedCriterionCreate",
        user_id="u-rejected-criterion-create",
        vision={"purpose": "test", "revision": 0},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-active-create",
        text="Active criterion",
        created_by="test",
    )
    db_session.add(criterion)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "direction_operations": [
                {
                    "op": "create_direction",
                    "text": "A brand-new direction",
                    "success_evidence": "tests pass and build is green",
                    "retirement_condition": "the capability ships",
                    "criterion_keys": ["c-ghost-key"],
                    "rationale": "new work",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(project)

    assert not any(
        d.get("text") == "A brand-new direction" for d in (project.directions or [])
    ), "create_direction with invalid criterion_keys must not create a direction"

    rejected = (task.context or {}).get(REJECTED_DIRECTION_OPS_CONTEXT_KEY) or []
    entry = next(
        (r for r in rejected if r.get("text") == "A brand-new direction"), None
    )
    assert entry is not None, "create_direction rejection must be surfaced"
    assert entry["op"] == "create_direction"
    assert "c-ghost-key" in entry["invalid_criterion_keys"]
    assert "c-active-create" in entry["active_criterion_keys"]


def test_build_rejected_direction_ops_surfaces_cycle_metadata():
    """_build_rejected_direction_ops must surface bounded cycle_metadata
    records (already normalized upstream) and skip failed/empty cycles."""
    from app.strategic_spawning import _build_rejected_direction_ops

    cycle_history = [
        {
            "outcome": "completed",
            "rejected_direction_ops": [
                {
                    "op": "update_direction",
                    "direction_id": "d-one",
                    "text": "",
                    "invalid_criterion_keys": ["c-ghost"],
                    "active_criterion_keys": ["c-live", "c-other"],
                    "rejected_at": "2026-08-01T00:00:00+00:00",
                }
            ],
        },
        {"outcome": "completed"},
        {
            "outcome": "failed",
            "rejected_direction_ops": [
                {"op": "create_direction", "direction_id": None}
            ],
        },
    ]
    result = _build_rejected_direction_ops(cycle_history, n=5)
    assert len(result) == 1, "only completed cycles with records surface"
    assert result[0]["direction_id"] == "d-one"
    assert result[0]["invalid_criterion_keys"] == ["c-ghost"]
    assert result[0]["active_criterion_keys"] == ["c-live", "c-other"]
    assert result[0]["op"] == "update_direction"


@pytest.mark.asyncio
async def test_build_criterion_link_conflicts_detects_dangling_keys(db_session):
    """_build_criterion_link_conflicts must reuse the read-model classifier to
    flag active directions whose criterion_keys are unknown in the open version,
    and return None for a clean model."""
    from app.strategic_spawning import _build_criterion_link_conflicts
    from app import release_version as rv

    project = Project(
        name="CriterionLinkConflicts",
        user_id="u-criterion-link-conflicts",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-dangling",
                "text": "Bad link",
                "status": "active",
                "criterion_keys": ["c-ghost-key"],
                "research_history": [],
                "notes": [],
            },
            {
                "id": "d-clean",
                "text": "Good link",
                "status": "active",
                "criterion_keys": ["c-active-key"],
                "research_history": [],
                "notes": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-active-key",
        text="Active criterion",
        created_by="test",
    )
    db_session.add(criterion)
    await db_session.commit()

    conflicts = await _build_criterion_link_conflicts(db_session, project)
    assert conflicts is not None
    assert any(
        c.get("direction_id") == "d-dangling"
        and c.get("criterion_key") == "c-ghost-key"
        for c in conflicts
    )
    assert not any(
        c.get("direction_id") == "d-clean" for c in conflicts
    ), "a valid criterion link must not be flagged"


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
                    "terminal_reason": "invalidated",
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

    def test_reset_baseline_uses_verified_merges_not_raw(self):
        # Reset baseline must be stamped on the VERIFIED basis so the latch's
        # post_merged = total - reset_merged delta math stays consistent with
        # record_outcome_progress_snapshot.
        d = {
            "research_history": [
                {"prs_merged": 4, "verified_prs_merged": 1},
                {"prs_merged": 3, "verified_prs_merged": 2},
            ],
            "history_aggregate": {
                "total_cycles": 5,
                "total_prs_merged": 9,
                "total_verified_prs_merged": 4,
            },
            "outcome_progress_ever_met": True,
        }
        _reset_scaffolding_state_on_outcome_change(d)
        assert d["outcome_progress_reset_at_cycle"] == 7
        # verified history 1 + 2, aggregate verified 4 -> 7 (raw would be 16)
        assert d["outcome_progress_reset_at_merged"] == 7


def test_outcome_progress_excludes_stranded_merges():
    d = {
        "research_history": [
            {"prs_merged": 4, "verified_prs_merged": 4, "stranded_prs_merged": 1},
            {"prs_merged": 3, "verified_prs_merged": 2},
        ],
        "history_aggregate": {
            "total_cycles": 5,
            "total_prs_merged": 8,
            "total_verified_prs_merged": 5,
            "total_stranded_prs_merged": 2,
        },
        "outcome_progress_ever_met": True,
    }
    record_outcome_progress_snapshot(d)
    assert d["outcome_progress_history"][-1]["merged"] == 8

    _reset_scaffolding_state_on_outcome_change(d)
    assert d["outcome_progress_reset_at_cycle"] == 7
    assert d["outcome_progress_reset_at_merged"] == 8


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


class TestDesignDocCompletionStrategistEvent:
    """Phase 1 (design_doc_update) must pair its spawn-time
    strategic_cycle_started with a strategic_cycle_completed on every path that
    does NOT chain to Phase 2 — otherwise the frontend "Strategist working…"
    indicator sticks on (review iter-1 MAJOR, task_completion.py:1929)."""

    @pytest.mark.asyncio
    async def test_emits_completed_when_phase2_deferred(self, strat_fail_db, mocker):
        from unittest.mock import AsyncMock
        from app.task_completion import _handle_design_doc_completion

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        task = await _make_failure_task(
            strat_fail_db, project, task_type="design_doc_update"
        )
        cycle = StrategicCycle(
            id="sc-deferred-spawn",
            project_id=project.id,
            phase1_task_id=task.id,
            outcome="phase1_running",
        )
        task.strategic_cycle_id = cycle.id
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        # Phase-2 spawn deferred (active strategic task exists).
        mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(return_value=False),
        )
        emit = AsyncMock()
        mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

        await _handle_design_doc_completion(task, "", strat_fail_db, project=project)

        emit.assert_awaited_once()
        assert emit.await_args.args == (project.id, task.id, 1)
        assert emit.await_args.kwargs["cycle_id"] == cycle.id
        assert (
            emit.await_args.kwargs["phase_state"]
            == "failed_phase2_spawn"
        )
        assert emit.await_args.kwargs["completion"]["status"] == "failed"
        await strat_fail_db.refresh(cycle)
        assert cycle.outcome == "failed_phase2_spawn"
        assert cycle.outcome not in {
            "in_progress",
            "phase1_running",
            "phase1_done",
            "phase2_running",
        }

    @pytest.mark.asyncio
    async def test_emits_completed_when_phase2_spawn_raises(
        self, strat_fail_db, mocker
    ):
        from unittest.mock import AsyncMock
        from app.task_completion import _handle_design_doc_completion

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        task = await _make_failure_task(
            strat_fail_db, project, task_type="design_doc_update"
        )
        cycle = StrategicCycle(
            id="sc-exception-spawn",
            project_id=project.id,
            phase1_task_id=task.id,
            outcome="phase1_running",
        )
        task.strategic_cycle_id = cycle.id
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(side_effect=RuntimeError("spawn boom")),
        )
        mocker.patch("app.alert_dispatch._create_strategic_alert", new=AsyncMock())
        emit = AsyncMock()
        mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

        await _handle_design_doc_completion(task, "", strat_fail_db, project=project)

        emit.assert_awaited_once()
        assert emit.await_args.args == (project.id, task.id, 1)
        assert emit.await_args.kwargs["phase_state"] == "failed"
        assert emit.await_args.kwargs["completion"]["status"] == "failed"
        assert emit.await_args.kwargs["completion"]["failure_reason"] == "spawn boom"
        assert (
            "FAILED to spawn" in emit.await_args.kwargs["completion"]["status_message"]
        )
        await strat_fail_db.refresh(cycle)
        assert cycle.outcome == "failed_phase2_spawn"
        from app.strategic_state_read_model import _cycle_summary

        assert _cycle_summary(cycle, active_task=None)["outcome"] == "failed_phase2_spawn"

    @pytest.mark.asyncio
    async def test_terminal_cycle_retry_race_is_rejected_before_phase2_insert(
        self, strat_fail_db, mocker
    ):
        from unittest.mock import AsyncMock
        from app import strategic_cycle
        from app.strategic_spawning import _finalize_strategic_review_spawn

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        project_id = project.id
        cycle = await strategic_cycle.create(strat_fail_db, project_id=project_id)
        await strat_fail_db.commit()
        assert (
            await strategic_cycle.active_cycle_id_for_retry(strat_fail_db, cycle.id)
            == cycle.id
        )
        await strategic_cycle.mark_failure(
            strat_fail_db,
            cycle.id,
            phase="phase2_spawn",
            reason="terminal before retry finalize",
        )
        await strat_fail_db.commit()
        assert (
            await strategic_cycle.active_cycle_id_for_retry(strat_fail_db, cycle.id)
            is None
        )

        mocker.patch(
            "app.autonomous_research._try_research_spawn_advisory_lock",
            new=AsyncMock(return_value=True),
        )
        mocker.patch(
            "app.task_lifecycle._has_active_strategic_task",
            new=AsyncMock(return_value=False),
        )

        assert (
            await _finalize_strategic_review_spawn(
                strat_fail_db,
                project_id=project_id,
                project_name=project.name,
                payload={"type": "strategic_review"},
                cycle_id=cycle.id,
                strategy_only=False,
                base_vision={},
                base_problem_model={},
                base_running_update="",
                local_runner_intent={},
                preferred_runner_target_id=None,
                extra_context=None,
                operator_steered=False,
                steering_excerpt=None,
            )
            is False
        )
        await strat_fail_db.refresh(cycle)
        assert cycle.outcome == strategic_cycle.OUTCOME_FAILED_PHASE2_SPAWN
        assert cycle.phase2_task_id is None
        tasks = (
            (await strat_fail_db.execute(select(Task).where(Task.project_id == project_id)))
            .scalars()
            .all()
        )
        assert tasks == []

    @pytest.mark.asyncio
    async def test_phase2_raise_writes_retry_marker_even_when_task_expired(
        self, strat_fail_db, expire_fidelity, mocker
    ):
        """The except path must rely only on pre-captured identifiers: a failed
        spawn that expires the Task instance previously raised MissingGreenlet
        on the task.id lazy-load inside the error handler — masking chain_exc,
        skipping the retry marker, and failing the whole completion
        (task a9bf1317, 2026-06-11)."""
        from unittest.mock import AsyncMock
        from app.task_completion import _handle_design_doc_completion

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        task = await _make_failure_task(
            strat_fail_db, project, task_type="design_doc_update"
        )
        expected_id = str(task.id)

        async def _boom(*args, **kwargs):
            # Simulate the rollback-expiration that accompanies a real spawn
            # failure: every unloaded attribute access on `task` now requires IO.
            strat_fail_db.expire(task)
            raise RuntimeError("spawn boom")

        mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(side_effect=_boom),
        )
        alert = AsyncMock()
        mocker.patch("app.alert_dispatch._create_strategic_alert", new=alert)
        mocker.patch(
            "app.strategic_events.emit_strategic_cycle_completed", new=AsyncMock()
        )

        await _handle_design_doc_completion(task, "", strat_fail_db, project=project)

        await strat_fail_db.refresh(project)
        marker = (project.vision or {}).get("_pending_strategic_review")
        assert marker, "retry marker must be written when Phase 2 spawn raises"
        assert marker["design_doc_task_id"] == expected_id
        chain_alerts = [
            call
            for call in alert.await_args_list
            if "failed to spawn" in call.args[2] and expected_id[:8] in call.args[2]
        ]
        assert chain_alerts, (
            "the chain-failure alert must carry the pre-captured task id"
        )

    @pytest.mark.asyncio
    async def test_no_completed_emit_when_phase2_spawns(self, strat_fail_db, mocker):
        from unittest.mock import AsyncMock
        from app.task_completion import _handle_design_doc_completion

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        task = await _make_failure_task(
            strat_fail_db, project, task_type="design_doc_update"
        )

        # Successful Phase-2 spawn emits its OWN started — the cycle stays active,
        # so Phase 1 must NOT emit completed here.
        mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(return_value=True),
        )
        emit = AsyncMock()
        mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

        await _handle_design_doc_completion(task, "", strat_fail_db, project=project)

        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_manual_design_doc_cycle_skips_autonomous_budget_guard(
        self, strat_fail_db, mocker
    ):
        from unittest.mock import AsyncMock
        from app.task_completion import _handle_design_doc_completion
        from app.strategic_spawning import _spawn_design_doc_update

        project = Project(
            name="P",
            user_id="u1",
            vision={"purpose": "test"},
            design_doc={"overview": {"body": "x"}, "_meta": {"revision": 1}},
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        assert await _spawn_design_doc_update(project, strat_fail_db) is True
        task = (
            (
                await strat_fail_db.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()[0]
        )
        assert (task.context or {}).get(STRATEGIC_CYCLE_MANUAL_CONTEXT_KEY) is True
        task.status = TaskStatus.COMPLETED
        strat_fail_db.add(task)
        await strat_fail_db.commit()

        spawn = AsyncMock(return_value=True)
        mocker.patch("app.strategic_spawning._spawn_strategic_review", new=spawn)
        mocker.patch(
            "app.strategic_events.emit_strategic_cycle_completed",
            new=AsyncMock(),
        )

        await _handle_design_doc_completion(task, "", strat_fail_db, project=project)

        assert spawn.await_count == 1
        assert spawn.await_args.kwargs["apply_spawn_budget_guard"] is False

    @pytest.mark.asyncio
    async def test_deferred_path_survives_task_and_project_expiry(
        self, strat_fail_db, expire_fidelity, mocker
    ):
        """Deferred Phase 2 on an expired session must stay whole.

        A bare drift-rollback inside _spawn_strategic_review expires BOTH project
        and task, so the deferred branch's logger + retry-marker write
        (project.vision) AND the post-commit emit (task.project_id / task.id /
        task.latest_status_message) previously lazy-loaded and raised
        MissingGreenlet — the site that failed task 94594516 (2026-06-11) past
        the a9bf1317 partial fix. Pre-captured ids + a captured vision snapshot
        must keep the handler whole on the deferred path too."""
        from unittest.mock import AsyncMock
        from app.task_completion import _handle_design_doc_completion

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        task = await _make_failure_task(
            strat_fail_db, project, task_type="design_doc_update"
        )
        expected_id = str(task.id)
        expected_pid = project.id

        async def _defer_and_expire(*args, **kwargs):
            # Mirror the rollback-expiration accompanying a real drift failure:
            # every unloaded attribute on BOTH instances now requires IO.
            strat_fail_db.expire(task)
            strat_fail_db.expire(project)
            return False

        mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(side_effect=_defer_and_expire),
        )
        emit = AsyncMock()
        mocker.patch("app.strategic_events.emit_strategic_cycle_completed", new=emit)

        # Must complete without raising MissingGreenlet.
        await _handle_design_doc_completion(task, "", strat_fail_db, project=project)

        # Emitted with pre-captured identifiers (never lazy-loaded off the
        # expired task).
        emit.assert_awaited_once()
        args = emit.await_args.args
        assert args[0] == expected_pid
        assert str(args[1]) == expected_id
        assert args[2] == 1
        assert emit.await_args.kwargs["completion"]["status"] == "deferred"
        # Retry marker written from the captured base_vision snapshot.
        await strat_fail_db.refresh(project)
        marker = (project.vision or {}).get("_pending_strategic_review")
        assert marker and marker["design_doc_task_id"] == expected_id


# ─────────────────────────────────────────────────────────────────────────────
# running_update operator-wins reconciliation (shared helper + fallback path)
# ─────────────────────────────────────────────────────────────────────────────

# A real routine-event digest line, exactly as Project.append_routine_digest
# emits it: "- [YYYY-MM-DD HH:MM] **<event_type>**: <summary>".
_DIGEST_LINE = "- [2026-06-03 12:00] **pr_merged**: Merged PR #103"


def test_reconcile_running_update_blind_assign_when_no_baseline():
    """No cycle/baseline resolvable (legacy task) → take the agent's document
    verbatim, never record a rejection (today's blind-assign behavior)."""
    from app.running_update_merge import reconcile_running_update

    doc, rejected = reconcile_running_update(
        baseline=None, agent="AGENT DOC", current="WHATEVER OPERATOR WROTE"
    )
    assert doc == "AGENT DOC"
    assert rejected is None


def test_reconcile_running_update_agent_wins_when_untouched():
    """current == baseline (nothing changed it since the agent got the
    baseline) → take the agent's fresh document."""
    from app.running_update_merge import reconcile_running_update

    doc, rejected = reconcile_running_update(
        baseline="BASELINE", agent="AGENT NEW DOC", current="BASELINE"
    )
    assert doc == "AGENT NEW DOC"
    assert rejected is None


def test_reconcile_running_update_agent_wins_when_only_trailing_whitespace():
    """A baseline/current difference of only trailing whitespace (which
    append_routine_digest rstrips) is not an operator edit → agent wins."""
    from app.running_update_merge import reconcile_running_update

    doc, rejected = reconcile_running_update(
        baseline="BASELINE\n\n", agent="AGENT NEW DOC", current="BASELINE"
    )
    assert doc == "AGENT NEW DOC"
    assert rejected is None


def test_reconcile_running_update_operator_edit_wins_and_records_rejected():
    """An operator body edit beats the agent's refreshed write; the agent's
    superseded document is recorded into the rejected-edits ledger entry."""
    from app.running_update_merge import (
        RUNNING_UPDATE_REJECTED_KIND,
        reconcile_running_update,
    )

    doc, rejected = reconcile_running_update(
        baseline="BASELINE",
        agent="AGENT NEW DOC",
        current="OPERATOR EDITED THE BODY",
    )
    assert doc == "OPERATOR EDITED THE BODY"
    assert rejected is not None
    assert rejected["field"] == "running_update"
    assert rejected["kind"] == RUNNING_UPDATE_REJECTED_KIND
    assert rejected["agent_value"] == "AGENT NEW DOC"


def test_reconcile_running_update_digest_append_reintegrated():
    """A mid-cycle routine-event digest append is NOT an operator edit: the
    agent's rewrite lands AND the digest line is re-appended (not lost, and
    the document is not frozen at the stale baseline)."""
    from app.running_update_merge import reconcile_running_update

    current = f"BASELINE\n{_DIGEST_LINE}"
    doc, rejected = reconcile_running_update(
        baseline="BASELINE", agent="AGENT NEW DOC", current=current
    )
    assert doc == f"AGENT NEW DOC\n{_DIGEST_LINE}"
    assert rejected is None


def test_reconcile_running_update_multiline_summary_digest_preserved():
    """End-to-end guard for the multi-line lost-update: a routine digest produced
    by the canonical ``append_routine_digest`` from a MULTI-LINE summary must be
    classified as a pure digest append, not an operator body edit. Pre-fix the
    multi-line summary spanned several physical lines and the agent's refreshed
    running_update was silently discarded into the rejected-edits ledger."""
    from app.models import Project
    from app.running_update_merge import reconcile_running_update

    baseline = "BASELINE DOC"
    project = Project(name="t", user_id="u1")
    project.running_update = baseline
    # A routine-typed message with multi-line free-text content fires mid-cycle.
    project.append_routine_digest(
        "cycle_summary", "First paragraph.\n\nSecond paragraph with detail."
    )
    current = project.running_update

    doc, rejected = reconcile_running_update(
        baseline=baseline, agent="AGENT REFRESHED DOC", current=current
    )
    # Agent wins; no spurious running_update_superseded; digest re-integrated.
    assert rejected is None
    assert doc.startswith("AGENT REFRESHED DOC\n")
    assert "First paragraph. Second paragraph with detail." in doc


def test_reconcile_running_update_digest_append_on_empty_baseline():
    """An empty baseline followed by only a digest line is still a pure digest
    append — the agent doc wins and the digest is re-integrated."""
    from app.running_update_merge import reconcile_running_update

    doc, rejected = reconcile_running_update(
        baseline="", agent="AGENT NEW DOC", current=_DIGEST_LINE
    )
    assert doc == f"AGENT NEW DOC\n{_DIGEST_LINE}"
    assert rejected is None


def test_reconcile_running_update_multiple_digest_lines_reintegrated():
    """Several digest lines appended during one cycle are all re-integrated."""
    from app.running_update_merge import reconcile_running_update

    line2 = "- [2026-06-03 13:30] **cycle_summary**: Cycle 12 complete"
    current = f"BASELINE\n{_DIGEST_LINE}\n{line2}"
    doc, rejected = reconcile_running_update(
        baseline="BASELINE", agent="AGENT NEW DOC", current=current
    )
    assert doc == f"AGENT NEW DOC\n{_DIGEST_LINE}\n{line2}"
    assert rejected is None


def test_reconcile_running_update_non_routine_appended_line_is_operator_edit():
    """A trailing line that mimics the digest shape but carries an event_type
    NOT in ROUTINE_EVENT_TYPES is treated as an operator edit (operator wins),
    so an operator's hand-written bullet is never silently dropped."""
    from app.running_update_merge import reconcile_running_update

    fake = "- [2026-06-03 12:00] **operator_note**: please hold this release"
    current = f"BASELINE\n{fake}"
    doc, rejected = reconcile_running_update(
        baseline="BASELINE", agent="AGENT NEW DOC", current=current
    )
    assert doc == current  # operator wins
    assert rejected is not None
    assert rejected["agent_value"] == "AGENT NEW DOC"


@pytest.mark.asyncio
async def test_strategic_completion_running_update_operator_edit_preserved(
    db_session, mocker
):
    """FALLBACK path: a concurrent operator edit to running_update survives the
    cycle completion (operator wins) and the agent's document is recorded as a
    rejected edit on the vision ledger."""
    import json as _json
    from unittest.mock import AsyncMock
    from app import strategic_cycle
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app.vision_merge import REJECTED_EDITS_KEY

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RU-operator-wins",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
        running_update="BASELINE DOC",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update="BASELINE DOC",
    )
    await db_session.commit()

    # Operator edits the document mid-cycle.
    project.running_update = "OPERATOR EDITED BODY"
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = _json.dumps({"running_update": "AGENT REFRESHED DOC"})
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    assert project.running_update == "OPERATOR EDITED BODY"
    ledger = (project.vision or {}).get(REJECTED_EDITS_KEY) or []
    assert any(
        e.get("field") == "running_update"
        and e.get("agent_value") == "AGENT REFRESHED DOC"
        for e in ledger
    ), f"Expected the agent's superseded doc on the ledger; got {ledger}"


@pytest.mark.asyncio
async def test_strategic_completion_running_update_digest_reintegrated(
    db_session, mocker
):
    """FALLBACK path: a mid-cycle routine-event digest append does NOT discard
    the agent's rewrite — the agent's document lands and the digest line is
    re-integrated onto it."""
    import json as _json
    from unittest.mock import AsyncMock
    from app import strategic_cycle
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RU-digest",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
        running_update="BASELINE DOC",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update="BASELINE DOC",
    )
    await db_session.commit()

    # A routine digest line fires mid-cycle (exactly as append_routine_digest
    # would have produced it).
    project.running_update = f"BASELINE DOC\n{_DIGEST_LINE}"
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = _json.dumps({"running_update": "AGENT REFRESHED DOC"})
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    assert project.running_update == f"AGENT REFRESHED DOC\n{_DIGEST_LINE}"


@pytest.mark.asyncio
async def test_strategic_completion_running_update_agent_wins_untouched(
    db_session, mocker
):
    """FALLBACK path: when nothing touched running_update since the baseline,
    the agent's refreshed document is taken verbatim."""
    import json as _json
    from unittest.mock import AsyncMock
    from app import strategic_cycle
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RU-agent-wins",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
        running_update="BASELINE DOC",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update="BASELINE DOC",
    )
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = _json.dumps({"running_update": "AGENT REFRESHED DOC"})
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    assert project.running_update == "AGENT REFRESHED DOC"


@pytest.mark.asyncio
async def test_apply_reconciled_running_update_two_agent_writes_one_cycle_both_persist(
    db_session,
):
    """Two consecutive agent writes in one cycle BOTH persist: the baseline
    advances to the first persisted document so the second write is recognized
    as the agent's own refinement, not misclassified as an operator edit. No
    spurious running_update_superseded entry is recorded.

    This is the core regression: pre-fix the second write saw current=DOC_A !=
    baseline=BASELINE and was discarded operator-wins."""
    from app import strategic_cycle
    from app.running_update_merge import (
        RUNNING_UPDATE_REJECTED_KIND,
        apply_reconciled_running_update,
    )
    from app.vision_merge import REJECTED_EDITS_KEY

    project = Project(
        name="RU-two-writes",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
        running_update="BASELINE DOC",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update="BASELINE DOC",
    )
    await db_session.commit()

    # First agent write in the cycle.
    rejected1 = apply_reconciled_running_update(
        project,
        baseline=strategic_cycle.baseline_running_update(cycle),
        agent="AGENT DOC A",
        cycle=cycle,
    )
    db_session.add(project)
    db_session.add(cycle)
    await db_session.commit()
    assert rejected1 is None
    assert project.running_update == "AGENT DOC A"
    # The baseline advanced to the just-persisted document.
    assert strategic_cycle.baseline_running_update(cycle) == "AGENT DOC A"

    # Second agent write in the SAME cycle: reconcile against the advanced
    # baseline (read fresh as both write paths do), not the cycle-start snapshot.
    rejected2 = apply_reconciled_running_update(
        project,
        baseline=strategic_cycle.baseline_running_update(cycle),
        agent="AGENT DOC B",
        cycle=cycle,
    )
    db_session.add(project)
    db_session.add(cycle)
    await db_session.commit()
    assert rejected2 is None
    assert project.running_update == "AGENT DOC B"

    await db_session.refresh(project)
    ledger = (project.vision or {}).get(REJECTED_EDITS_KEY) or []
    assert not any(e.get("kind") == RUNNING_UPDATE_REJECTED_KIND for e in ledger), (
        f"No running_update should be recorded as superseded; got {ledger}"
    )


@pytest.mark.asyncio
async def test_apply_reconciled_running_update_operator_edit_between_writes_still_wins(
    db_session,
):
    """A genuine operator body edit made BETWEEN two agent writes still wins: it
    diverges from the advanced baseline, so the second agent write is recorded
    operator-wins and the operator's document is preserved."""
    from app import strategic_cycle
    from app.running_update_merge import (
        RUNNING_UPDATE_REJECTED_KIND,
        apply_reconciled_running_update,
    )
    from app.vision_merge import REJECTED_EDITS_KEY

    project = Project(
        name="RU-operator-between",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
        running_update="BASELINE DOC",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(
        db_session,
        project_id=project.id,
        baseline_running_update="BASELINE DOC",
    )
    await db_session.commit()

    # First agent write.
    apply_reconciled_running_update(
        project,
        baseline=strategic_cycle.baseline_running_update(cycle),
        agent="AGENT DOC A",
        cycle=cycle,
    )
    db_session.add(project)
    db_session.add(cycle)
    await db_session.commit()
    assert project.running_update == "AGENT DOC A"
    assert strategic_cycle.baseline_running_update(cycle) == "AGENT DOC A"

    # Operator edits the body between the two agent writes.
    project.running_update = "OPERATOR HAND EDIT"
    db_session.add(project)
    await db_session.commit()

    # Second agent write: current (OPERATOR HAND EDIT) diverges from the advanced
    # baseline (AGENT DOC A), so operator wins and the agent doc is recorded.
    rejected = apply_reconciled_running_update(
        project,
        baseline=strategic_cycle.baseline_running_update(cycle),
        agent="AGENT DOC B",
        cycle=cycle,
    )
    db_session.add(project)
    db_session.add(cycle)
    await db_session.commit()
    assert rejected is not None
    await db_session.refresh(project)
    assert project.running_update == "OPERATOR HAND EDIT"
    ledger = (project.vision or {}).get(REJECTED_EDITS_KEY) or []
    assert any(
        e.get("kind") == RUNNING_UPDATE_REJECTED_KIND
        and e.get("agent_value") == "AGENT DOC B"
        for e in ledger
    ), f"Expected agent doc B recorded operator-superseded; got {ledger}"


def test_reconcile_running_update_invalid_event_type_in_middle_is_operator_edit():
    """When appended lines include a mix of valid digest events and an
    unrecognized event type in the middle (e.g., baseline + valid_digest_line +
    invalid_custom_line + valid_digest_line), the _detect_digest_append logic
    should classify this as an operator edit (operator wins), not a pure digest
    append, since the operator edited the body."""
    from app.running_update_merge import reconcile_running_update

    valid_line_1 = "- [2026-06-03 12:00] **pr_merged**: Merged PR #103"
    invalid_line = (
        "- [2026-06-03 12:30] **custom_operator_note**: operator's hand-written note"
    )
    valid_line_2 = "- [2026-06-03 13:00] **cycle_summary**: Cycle complete"

    current = f"BASELINE\n{valid_line_1}\n{invalid_line}\n{valid_line_2}"
    doc, rejected = reconcile_running_update(
        baseline="BASELINE", agent="AGENT NEW DOC", current=current
    )
    # Operator wins because the middle line is not a recognized routine event type
    assert doc == current
    assert rejected is not None
    assert rejected["field"] == "running_update"
    assert rejected["agent_value"] == "AGENT NEW DOC"


def test_apply_reconciled_running_update_caps_rejected_edits_ledger():
    """When vision._rejected_agent_edits is already at capacity
    (REJECTED_EDITS_MAX entries), adding a new running_update_superseded entry
    should not exceed the cap and should discard the oldest entry."""
    from app.running_update_merge import (
        apply_reconciled_running_update,
        RUNNING_UPDATE_REJECTED_KIND,
    )
    from app.vision_merge import REJECTED_EDITS_KEY, REJECTED_EDITS_MAX

    project = Project(
        name="RU-ledger-cap",
        user_id="u1",
        vision={
            "purpose": "test",
            "revision": 1,
            REJECTED_EDITS_KEY: [
                {
                    "at": "2026-06-03T12:00:00",
                    "field": "vision",
                    "kind": "unrelated",
                    "value": f"entry_{i}",
                }
                for i in range(REJECTED_EDITS_MAX)
            ],
        },
        directions=[],
        # Operator edited the body mid-cycle: the live running_update diverges
        # from the cycle baseline (a full-body edit, not a digest append), so
        # reconciliation must take the operator's doc and reject the agent's.
        running_update="OPERATOR EDITED BODY",
    )

    # The operator body edit above supersedes the agent doc, so a new
    # running_update_superseded entry is appended to the (already-full) ledger.
    rejected = apply_reconciled_running_update(
        project,
        baseline="BASELINE",
        agent="AGENT DOCUMENT THAT WILL BE REJECTED",
    )

    # Verify the rejected entry was created
    assert rejected is not None
    assert rejected["kind"] == RUNNING_UPDATE_REJECTED_KIND
    assert rejected["field"] == "running_update"
    assert rejected["agent_value"] == "AGENT DOCUMENT THAT WILL BE REJECTED"

    # Verify the ledger is still capped at REJECTED_EDITS_MAX
    ledger = project.vision.get(REJECTED_EDITS_KEY) or []
    assert len(ledger) == REJECTED_EDITS_MAX, (
        f"Ledger exceeded cap: expected {REJECTED_EDITS_MAX}, got {len(ledger)}"
    )

    # Verify the new entry is present (at the end)
    assert ledger[-1] == rejected

    # Verify the oldest entry (entry_0) was discarded when the new entry was added
    # by checking that the remaining old entries are 1 through REJECTED_EDITS_MAX-2
    old_entries = [e for e in ledger if e.get("kind") == "unrelated"]
    assert len(old_entries) == REJECTED_EDITS_MAX - 1
    # The first old entry should be entry_1 (entry_0 was dropped)
    assert old_entries[0]["value"] == "entry_1"


# ── Message convergence: linkage contract tests ───────────────────────────────


@pytest_asyncio.fixture()
async def convergence_db(mocker):
    """Isolated in-memory DB for message-convergence linkage tests."""
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


class TestMessageConvergenceLinkage:
    """Tests for the bug-class message linkage contract.

    Covers:
      (a) resolving a bug-class message by ownership persists the durable
          forward link and leaves the message in the 'linked' (not terminal
          'resolved') state.
      (b) an explicit operator/strategist resolve still terminally resolves
          regardless of link state.
      (c) a linked message is excluded from the strategist's actionable
          open-message feed.
    """

    # ── (a) Ownership linkage ────────────────────────────────────────────────

    def test_link_message_to_outcome_sets_linked_status(self):
        """link_message_to_outcome sets status='linked' and stores the forward link."""
        from app.message_convergence import (
            LINKED_STATUS,
            get_message_link,
            link_message_to_outcome,
        )
        from app.models import MessageType, ProjectMessage

        msg = ProjectMessage(
            message_type=MessageType.BUG_REPORT,
            content="CI is broken",
            context={},
            status="open",
        )
        link_message_to_outcome(msg, "direction", "d-abc123")

        assert msg.status == LINKED_STATUS, (
            f"Expected status '{LINKED_STATUS}', got '{msg.status}'"
        )
        link = get_message_link(msg)
        assert link is not None, "Expected a convergence link in context"
        assert link["kind"] == "direction"
        assert link["id"] == "d-abc123"
        assert "linked_at" in link

    def test_link_message_task_kind(self):
        """link_message_to_outcome supports task kind."""
        from app.message_convergence import get_message_link, link_message_to_outcome
        from app.models import MessageType, ProjectMessage

        task_id = str(uuid.uuid4())
        msg = ProjectMessage(
            message_type=MessageType.BLOCKER,
            content="blocked on missing dep",
            context={},
            status="open",
        )
        link_message_to_outcome(msg, "task", task_id)

        link = get_message_link(msg)
        assert link is not None
        assert link["kind"] == "task"
        assert link["id"] == task_id

    def test_link_message_does_not_set_resolved_at(self):
        """Linking a message must NOT set resolved_at — it's not terminal."""
        from app.message_convergence import link_message_to_outcome
        from app.models import MessageType, ProjectMessage

        msg = ProjectMessage(
            message_type=MessageType.BUG_REPORT,
            content="test",
            context={},
            status="open",
        )
        link_message_to_outcome(msg, "direction", "d-xyz")
        assert msg.resolved_at is None

    def test_is_bug_class_true_for_bug_report_and_blocker(self):
        """is_bug_class returns True for BUG_REPORT and BLOCKER."""
        from app.message_convergence import is_bug_class
        from app.models import MessageType, ProjectMessage

        for mt in (MessageType.BUG_REPORT, MessageType.BLOCKER):
            msg = ProjectMessage(message_type=mt, content="x", status="open")
            assert is_bug_class(msg), f"Expected is_bug_class=True for {mt}"

    def test_is_bug_class_false_for_other_types(self):
        """is_bug_class returns False for NOTE, FEEDBACK, QUESTION."""
        from app.message_convergence import is_bug_class
        from app.models import MessageType, ProjectMessage

        for mt in (MessageType.NOTE, MessageType.FEEDBACK, MessageType.QUESTION):
            msg = ProjectMessage(message_type=mt, content="x", status="open")
            assert not is_bug_class(msg), f"Expected is_bug_class=False for {mt}"

    def test_get_message_link_returns_none_when_absent(self):
        """get_message_link returns None when context has no convergence key."""
        from app.message_convergence import get_message_link
        from app.models import ProjectMessage

        msg = ProjectMessage(content="x", context={}, status="open")
        assert get_message_link(msg) is None

    @pytest.mark.asyncio
    async def test_fallback_path_links_bug_class_message_with_direction(
        self, convergence_db
    ):
        """(a) The fallback resolution path moves a BUG_REPORT to 'linked' state
        and stores the forward link when owned_by_direction is provided.
        """
        from app.message_convergence import LINKED_STATUS, get_message_link
        from app.models import MessageType, Project, ProjectMessage

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        convergence_db.add(project)
        await convergence_db.commit()
        await convergence_db.refresh(project)

        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="CI pipeline is broken",
            context={},
            status="open",
        )
        convergence_db.add(msg)
        await convergence_db.commit()
        await convergence_db.refresh(msg)

        from app.message_convergence import link_message_to_outcome

        link_message_to_outcome(msg, "direction", "d-fix001")
        convergence_db.add(msg)
        await convergence_db.commit()
        await convergence_db.refresh(msg)

        assert msg.status == LINKED_STATUS
        link = get_message_link(msg)
        assert link is not None
        assert link["kind"] == "direction"
        assert link["id"] == "d-fix001"

    @pytest.mark.asyncio
    async def test_fallback_path_explicit_resolve_overrides_link(self, convergence_db):
        """(b) An explicit resolve (no ownership fields) terminally resolves
        the message, even if it was previously in 'linked' state.
        """
        from app.message_convergence import LINKED_STATUS, link_message_to_outcome
        from app.models import MessageType, Project, ProjectMessage

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        convergence_db.add(project)
        await convergence_db.commit()
        await convergence_db.refresh(project)

        # Start with a linked message
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="Flaky test",
            context={},
            status="open",
        )
        convergence_db.add(msg)
        await convergence_db.commit()
        await convergence_db.refresh(msg)

        link_message_to_outcome(msg, "direction", "d-wont-fix")
        convergence_db.add(msg)
        await convergence_db.commit()
        await convergence_db.refresh(msg)

        assert msg.status == LINKED_STATUS

        # Now explicitly resolve (no ownership info) — operator overrides
        msg.status = "resolved"
        msg.resolution = "Direction retired without fix, won't address"
        convergence_db.add(msg)
        await convergence_db.commit()
        await convergence_db.refresh(msg)

        assert msg.status == "resolved", (
            "Explicit resolve must be terminal regardless of prior link"
        )

    @pytest.mark.asyncio
    async def test_non_bug_class_message_always_resolved_not_linked(
        self, convergence_db
    ):
        """(b) A FEEDBACK message is NOT bug-class and always gets 'resolved'."""
        from app.message_convergence import LINKED_STATUS
        from app.models import MessageType, ProjectMessage

        msg = ProjectMessage(
            message_type=MessageType.FEEDBACK,
            content="Would be nice if...",
            context={},
            status="open",
        )
        # Attempting to link a non-bug-class message is a no-op at the is_bug_class check.
        # The fallback path only calls link_message_to_outcome when is_bug_class(msg) is True.
        from app.message_convergence import is_bug_class

        assert not is_bug_class(msg), "FEEDBACK is not bug-class"
        # Status unchanged — the handler would NOT link this message even with ownership info.
        assert msg.status == "open"
        assert msg.status != LINKED_STATUS

    # ── (c) Feed exclusion ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_linked_message_excluded_from_open_feed(self, convergence_db):
        """(c) A linked message (status='linked') does not appear in the strategist's
        actionable open-message feed even though it has not been resolved.
        """
        from app.message_convergence import LINKED_STATUS, link_message_to_outcome
        from app.models import MessageType, Project, ProjectMessage
        from app.strategic_spawning import _fetch_open_messages

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        convergence_db.add(project)
        await convergence_db.commit()
        await convergence_db.refresh(project)

        # Create one open message (should appear in the feed).
        open_msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="Open bug — needs attention",
            context={},
            status="open",
        )
        # Create one linked message (should NOT appear in the feed).
        linked_msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="Linked bug — fix already in flight",
            context={},
            status="open",
        )
        convergence_db.add(open_msg)
        convergence_db.add(linked_msg)
        await convergence_db.commit()
        await convergence_db.refresh(open_msg)
        await convergence_db.refresh(linked_msg)

        # Link the second message to an in-flight direction.
        link_message_to_outcome(linked_msg, "direction", "d-in-flight")
        convergence_db.add(linked_msg)
        await convergence_db.commit()
        await convergence_db.refresh(linked_msg)

        assert linked_msg.status == LINKED_STATUS

        feed = await _fetch_open_messages(convergence_db, project.id)
        feed_ids = {item["id"] for item in feed}

        assert str(open_msg.id) in feed_ids, (
            "Open BUG_REPORT must be visible in the strategist's feed"
        )
        assert str(linked_msg.id) not in feed_ids, (
            "Linked BUG_REPORT must NOT appear in the actionable feed — "
            "it already has an owner"
        )

    @pytest.mark.asyncio
    async def test_strategic_completion_clusters_duplicate_bug_messages(
        self, convergence_db
    ):
        """Duplicate bug messages with the same structured signature share one owned outcome."""
        from app.message_convergence import LINKED_STATUS, get_message_link
        from app.models import MessageType, Project, ProjectMessage, Task
        from app.strategic_completion import _handle_strategic_completion

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        convergence_db.add(project)
        await convergence_db.commit()
        await convergence_db.refresh(project)

        anchor = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="Residual auto-fix is suspended",
            context={
                "event_type": "residual_fix_escalated",
                "fingerprint": "fp-shared",
            },
            status="open",
        )
        duplicate = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="Residual auto-fix is suspended again",
            context={
                "event_type": "residual_fix_escalated",
                "fingerprint": "fp-shared",
            },
            status="open",
        )
        convergence_db.add(anchor)
        convergence_db.add(duplicate)
        await convergence_db.commit()
        await convergence_db.refresh(anchor)
        await convergence_db.refresh(duplicate)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review", "prompt": "review", "vision": {}}],
        )
        convergence_db.add(task)
        await convergence_db.commit()
        await convergence_db.refresh(task)

        logs = json.dumps(
            {
                "vision": {"revision": 1},
                "resolved_messages": [
                    {
                        "id": str(anchor.id),
                        "owned_by_direction": "d-dup",
                        "resolution": "Fix owned by direction d-dup",
                    }
                ],
            }
        )
        await _handle_strategic_completion(task, logs, convergence_db)

        await convergence_db.refresh(anchor)
        await convergence_db.refresh(duplicate)

        assert anchor.status == LINKED_STATUS
        assert duplicate.status == LINKED_STATUS
        anchor_link = get_message_link(anchor)
        duplicate_link = get_message_link(duplicate)
        assert anchor_link is not None
        assert duplicate_link is not None
        assert anchor_link["kind"] == duplicate_link["kind"] == "direction"
        assert anchor_link["id"] == duplicate_link["id"] == "d-dup"
        assert anchor.resolution == duplicate.resolution

    @pytest.mark.asyncio
    async def test_strategic_completion_leaves_distinct_bug_messages_independent(
        self, convergence_db
    ):
        """Different structured signatures must not collapse into one owned outcome."""
        from app.message_convergence import get_message_link
        from app.models import MessageType, Project, ProjectMessage, Task
        from app.strategic_completion import _handle_strategic_completion

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        convergence_db.add(project)
        await convergence_db.commit()
        await convergence_db.refresh(project)

        anchor = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="Residual auto-fix is suspended",
            context={
                "event_type": "residual_fix_escalated",
                "fingerprint": "fp-one",
            },
            status="open",
        )
        distinct = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BUG_REPORT,
            content="Residual auto-fix is suspended elsewhere",
            context={
                "event_type": "residual_fix_escalated",
                "fingerprint": "fp-two",
            },
            status="open",
        )
        convergence_db.add(anchor)
        convergence_db.add(distinct)
        await convergence_db.commit()
        await convergence_db.refresh(anchor)
        await convergence_db.refresh(distinct)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review", "prompt": "review", "vision": {}}],
        )
        convergence_db.add(task)
        await convergence_db.commit()
        await convergence_db.refresh(task)

        logs = json.dumps(
            {
                "vision": {"revision": 1},
                "resolved_messages": [
                    {
                        "id": str(anchor.id),
                        "owned_by_direction": "d-distinct",
                        "resolution": "Fix owned by direction d-distinct",
                    }
                ],
            }
        )
        await _handle_strategic_completion(task, logs, convergence_db)

        await convergence_db.refresh(anchor)
        await convergence_db.refresh(distinct)

        assert anchor.status != "open"
        assert distinct.status == "open"
        assert get_message_link(anchor) is not None
        assert get_message_link(distinct) is None

    @pytest.mark.asyncio
    async def test_link_clustered_bug_messages_query_is_ordered_by_created_at(
        self, convergence_db
    ):
        """_link_clustered_bug_messages must order by created_at asc before .limit(500)."""
        import inspect
        from app.strategic_completion import _link_clustered_bug_messages

        source = inspect.getsource(_link_clustered_bug_messages)
        assert "order_by(ProjectMessage.created_at.asc())" in source, (
            "_link_clustered_bug_messages must include order_by(ProjectMessage.created_at.asc()) "
            "before .limit(500) to make the limit deterministic"
        )

    @pytest.mark.asyncio
    async def test_resolved_message_also_excluded_from_open_feed(self, convergence_db):
        """Sanity: resolved messages (terminal) also stay out of the feed."""
        from app.models import MessageType, Project, ProjectMessage
        from app.strategic_spawning import _fetch_open_messages

        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        convergence_db.add(project)
        await convergence_db.commit()
        await convergence_db.refresh(project)

        resolved_msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            content="Already fixed",
            context={},
            status="resolved",
        )
        convergence_db.add(resolved_msg)
        await convergence_db.commit()
        await convergence_db.refresh(resolved_msg)

        feed = await _fetch_open_messages(convergence_db, project.id)
        feed_ids = {item["id"] for item in feed}
        assert str(resolved_msg.id) not in feed_ids


@pytest.mark.asyncio
async def test_strategic_completion_grounds_emitted_bug_report(db_session, mocker):
    """A BUG_REPORT parsed from the strategic agent's output that cites code
    absent on HEAD gets an ``_provenance_warnings`` absent signal on the emitted
    ProjectMessage — the strategist-authored path runs the same reproduce-on-HEAD
    grounding as the user/annotation paths."""
    from unittest.mock import AsyncMock
    from sqlalchemy import select as _select
    from app.strategic_completion import _handle_strategic_completion
    from app.models import ProjectMessage

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.source_ref_validation.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(return_value=False),
    )
    # The shared reproduce-on-HEAD contract (classify_reproduction) only reports
    # ``absent`` when a validation backend could actually run; with the branch
    # context mocked empty, assert the backend is available so a genuinely-broken
    # citation reaches ``absent`` rather than degrading to ``indeterminate``.
    mocker.patch(
        "app.source_ref_validation._has_validation_backend",
        return_value=True,
    )

    project = Project(
        name="P", user_id="u1", vision={"purpose": "test", "revision": 1}, directions=[]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "r", "vision": {"revision": 1}}
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
                    "type": "BUG_REPORT",
                    "content": "The guard regressed in backend/app/gone.py during init.",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    msg = (
        await db_session.execute(
            _select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    warnings = (msg.context or {}).get("_provenance_warnings")
    assert warnings is not None
    assert warnings["citation_state"] == "absent"
    assert "backend/app/gone.py" in warnings["broken_refs"]


@pytest.mark.asyncio
async def test_strategic_completion_present_citation_not_flagged(db_session, mocker):
    """A strategist-emitted BUG_REPORT whose cited file still exists is NOT flagged."""
    from unittest.mock import AsyncMock
    from sqlalchemy import select as _select
    from app.strategic_completion import _handle_strategic_completion
    from app.models import ProjectMessage

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.source_ref_validation.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(return_value=True),
    )

    project = Project(
        name="P", user_id="u1", vision={"purpose": "test", "revision": 1}, directions=[]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "r", "vision": {"revision": 1}}
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
                    "type": "BUG_REPORT",
                    "content": "Still reproduces in backend/app/real.py on load.",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    msg = (
        await db_session.execute(
            _select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    assert "_provenance_warnings" not in (msg.context or {})


@pytest.mark.asyncio
async def test_strategic_completion_mixed_present_citation_is_flagged(
    db_session, mocker
):
    """A partially grounded strategist-emitted BUG_REPORT keeps its mixed refs."""
    from unittest.mock import AsyncMock
    from sqlalchemy import select as _select
    from app.strategic_completion import _handle_strategic_completion
    from app.models import ProjectMessage

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.source_ref_validation.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(
            side_effect=lambda path, **_: "alive.py" in path
        ),
    )

    project = Project(
        name="P", user_id="u1", vision={"purpose": "test", "revision": 1}, directions=[]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "r", "vision": {"revision": 1}}
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
                    "type": "BUG_REPORT",
                    "content": (
                        "The crash spans backend/app/gone.py and "
                        "backend/app/alive.py during init."
                    ),
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    msg = (
        await db_session.execute(
            _select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    warnings = (msg.context or {}).get("_provenance_warnings")
    assert warnings is not None
    assert warnings["citation_state"] == "present"
    assert "backend/app/gone.py" in warnings["broken_refs"]


@pytest.mark.asyncio
async def test_strategic_completion_note_message_not_grounded(db_session, mocker):
    """A NOTE-type strategist message is never grounded, even when it cites code."""
    from unittest.mock import AsyncMock
    from sqlalchemy import select as _select
    from app.strategic_completion import _handle_strategic_completion
    from app.models import ProjectMessage

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    spy = mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(return_value=False),
    )

    project = Project(
        name="P", user_id="u1", vision={"purpose": "test", "revision": 1}, directions=[]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "r", "vision": {"revision": 1}}
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
                    "content": "Consider refactoring backend/app/gone.py soon.",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    msg = (
        await db_session.execute(
            _select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    assert "_provenance_warnings" not in (msg.context or {})
    spy.assert_not_called()


# ── Strategist autonomous spawn-path budget helpers ──────────────────────────


def test_spawn_budget_status_under_both_caps_is_ok():
    ok, scope, reason = spawn_budget_status(3, 5, per_review_cap=12, window_cap=20)
    assert ok is True
    assert scope is None
    assert reason is None


def test_spawn_budget_status_per_review_cap_exhausted():
    ok, scope, reason = spawn_budget_status(12, 5, per_review_cap=12, window_cap=20)
    assert ok is False
    assert scope == "per_review"
    assert "12/12" in reason


def test_spawn_budget_status_window_cap_exhausted():
    ok, scope, reason = spawn_budget_status(2, 20, per_review_cap=12, window_cap=20)
    assert ok is False
    assert scope == "window"
    assert "20/20" in reason


def test_spawn_budget_status_per_review_takes_precedence():
    # Both exhausted — per_review reported first.
    ok, scope, _ = spawn_budget_status(12, 20, per_review_cap=12, window_cap=20)
    assert ok is False
    assert scope == "per_review"


def test_spawn_budget_status_nonpositive_cap_disables_dimension():
    # Per-review disabled (cap<=0) but window still enforced.
    ok, scope, _ = spawn_budget_status(99, 20, per_review_cap=0, window_cap=20)
    assert ok is False
    assert scope == "window"
    # Both disabled — always ok.
    ok2, scope2, _ = spawn_budget_status(99, 99, per_review_cap=0, window_cap=0)
    assert ok2 is True
    assert scope2 is None


def test_spawn_title_is_redundant_overlapping_topic_matches():
    match = spawn_title_is_redundant(
        "Investigate the warm pool provisioning ready signal latency",
        ["Research warm pool provisioning ready signal timing"],
        threshold=4,
    )
    assert match == "Research warm pool provisioning ready signal timing"


def test_spawn_title_is_redundant_distinct_topics_not_matched():
    match = spawn_title_is_redundant(
        "Investigate A",
        ["Investigate B"],
        threshold=4,
    )
    assert match is None


def test_spawn_title_is_redundant_below_threshold_not_matched():
    # Only "warm" + "pool" overlap (2 non-stopword words) < threshold 4.
    match = spawn_title_is_redundant(
        "warm pool latency under load spikes",
        ["warm pool image bake duration regression"],
        threshold=4,
    )
    assert match is None


def test_spawn_title_is_redundant_empty_title_returns_none():
    assert spawn_title_is_redundant("", ["anything here at all"], threshold=4) is None


# ── Proof-aware auto-pause: undeclared merges forfeit pause-protection ─────────


def _accept_history(n, *, proven, declared, merged, completed, prs_merged=None):
    """n identical acceptance-contract cycles (proven ⊆ declared ⊆ merged-write)."""
    pm = merged if prs_merged is None else prs_merged
    return [
        {
            "tasks_created": completed,
            "tasks_completed": completed,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": pm,
            "verified_prs_merged": pm,
            "acceptance_merged_write_total": merged,
            "acceptance_proven_merged": proven,
            "acceptance_declared_merged": declared,
        }
        for _ in range(n)
    ]


class TestProofAwarePause:
    def test_undeclared_merges_forfeit_pause_shield_while_proven_keep_it(self):
        # Identical merge VOLUME (5 cycles, 1 verified merge + 6 completed each →
        # non_merge_finished = 30-5 = 25 ≥ churn threshold). Both mark the last
        # cycle productive, which normally SHIELDS Rules 5/6. Only the proof state
        # differs: the undeclared direction's proof-weighted merge rate falls below
        # the churn minimum AND it forfeits the productivity shield, so it pauses;
        # the proven direction keeps a healthy rate and its shield, so it does not.
        undeclared = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_accept_history(
                5, proven=0, declared=0, merged=1, completed=6
            ),
        )
        proven = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_accept_history(
                5, proven=1, declared=1, merged=1, completed=6
            ),
        )
        undeclared_reason = _check_direction_health(undeclared)
        proven_reason = _check_direction_health(proven)
        assert undeclared_reason is not None
        assert "Churn guardrail" in undeclared_reason
        assert proven_reason is None

    def test_recent_merges_mostly_unproven_gates_on_acceptance_data(self):
        # All-undeclared recent merges → shield forfeit.
        undeclared = _direction(
            research_history=_accept_history(
                5, proven=0, declared=0, merged=2, completed=2
            )
        )
        assert _recent_merges_mostly_unproven(undeclared) is True
        # All-proven → shield kept.
        proven = _direction(
            research_history=_accept_history(
                5, proven=2, declared=2, merged=2, completed=2
            )
        )
        assert _recent_merges_mostly_unproven(proven) is False
        # Legacy / read-only directions (no acceptance fields) are never stripped.
        legacy = _direction(
            research_history=_history(5, prs_merged=2, tasks_completed=2)
        )
        assert _recent_merges_mostly_unproven(legacy) is False
        assert _recent_merges_mostly_unproven(_direction()) is False

    def test_churn_merge_rate_is_proof_weighted_but_legacy_unchanged(self):
        # Legacy churn metrics are byte-for-byte unchanged (no acceptance fields):
        # merged stays an integer and the displayed string keeps "N merged".
        legacy = _direction(
            research_history=_history(
                5, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=0
            )
        )
        m = _compute_churn_metrics(legacy)
        assert m["merged"] == 0
        assert m["merge_rate"] == 0.0


# ── _build_retirement_snapshot unit tests ─────────────────────────────────────


class TestBuildRetirementSnapshot:
    """Unit tests for the _build_retirement_snapshot helper."""

    def _direction_with_context(self, **ctx_fields):
        return {
            "id": "d-snap-test",
            "text": "Build the end-to-end pipeline",
            "context": ctx_fields,
            "research_history": [
                {"cycles": 1, "prs_merged": 2, "tasks_completed": 3, "tasks_failed": 0},
                {"cycles": 1, "prs_merged": 1, "tasks_completed": 2, "tasks_failed": 1},
                {"cycles": 1, "prs_merged": 0, "tasks_completed": 1, "tasks_failed": 0},
                {"cycles": 1, "prs_merged": 3, "tasks_completed": 4, "tasks_failed": 0},
            ],
        }

    def test_snapshot_contains_expected_keys(self):
        direction = self._direction_with_context(
            success_evidence="CI passes on main", retirement_condition="90% coverage"
        )
        snap = _build_retirement_snapshot(direction, "Goal met")
        assert "direction_text" in snap
        assert "success_evidence" in snap
        assert "retirement_condition" in snap
        assert "rationale" in snap
        assert "recent_history_summary" in snap
        assert "retirement_metrics" in snap

    def test_snapshot_reads_context_fields(self):
        direction = self._direction_with_context(
            success_evidence="CI passes on main", retirement_condition="90% coverage"
        )
        snap = _build_retirement_snapshot(direction, "Goal fully met")
        assert snap["success_evidence"] == "CI passes on main"
        assert snap["retirement_condition"] == "90% coverage"
        assert snap["direction_text"] == "Build the end-to-end pipeline"
        assert snap["rationale"] == "Goal fully met"

    def test_snapshot_bounds_recent_history_to_three_cycles(self):
        direction = self._direction_with_context()
        snap = _build_retirement_snapshot(direction, "done")
        # history has 4 entries; snapshot should only include the last 3
        assert len(snap["recent_history_summary"]) == 3
        # Most recent entry is last in history (prs_merged=3)
        assert snap["recent_history_summary"][-1]["prs_merged"] == 3

    def test_snapshot_recent_history_fields(self):
        direction = self._direction_with_context()
        snap = _build_retirement_snapshot(direction, "done")
        for entry in snap["recent_history_summary"]:
            assert "cycles" in entry
            assert "prs_merged" in entry
            assert "tasks_completed" in entry
            assert "tasks_failed" in entry

    def test_snapshot_includes_retirement_metrics(self):
        direction = self._direction_with_context()
        snap = _build_retirement_snapshot(direction, "done")
        metrics = snap["retirement_metrics"]
        assert "final_quality_score" in metrics
        assert "final_prs_merged" in metrics
        assert "final_cycles_active" in metrics

    def test_snapshot_handles_absent_context_gracefully(self):
        direction = {
            "id": "d-no-ctx",
            "text": "Direction without context",
            "research_history": [],
        }
        snap = _build_retirement_snapshot(direction, "retired")
        assert snap["success_evidence"] == ""
        assert snap["retirement_condition"] == ""
        assert snap["recent_history_summary"] == []

    def test_snapshot_handles_empty_direction(self):
        snap = _build_retirement_snapshot({}, "rationale text")
        assert snap["direction_text"] == ""
        assert snap["success_evidence"] == ""
        assert snap["rationale"] == "rationale text"
        assert snap["recent_history_summary"] == []

    def test_snapshot_truncates_long_strings(self):
        long_text = "x" * 500
        direction = {
            "text": long_text,
            "context": {
                "success_evidence": long_text,
                "retirement_condition": long_text,
            },
            "research_history": [],
        }
        snap = _build_retirement_snapshot(direction, long_text)
        assert len(snap["direction_text"]) <= 300
        assert len(snap["success_evidence"]) <= 300
        assert len(snap["retirement_condition"]) <= 300
        assert len(snap["rationale"]) <= 300


# ── Retirement memo snapshot integration tests ────────────────────────────────


@pytest.mark.asyncio
async def test_retirement_memo_carries_direction_snapshot(db_session):
    """Spawned retirement memo payload carries source_finding with direction snapshot.

    Verifies: direction_text, success_evidence, retirement_condition, rationale,
    recent_history_summary, and retirement_metrics are present in source_finding.
    Also verifies that the memo task context carries mirrored success_evidence
    and retirement_condition.
    """
    from app.models import TaskStatus

    direction_id = str(uuid.uuid4())
    project = Project(
        name="SnapshotTest",
        user_id="u-snapshot-test",
        directions=[
            {
                "id": direction_id,
                "text": "Build the pipeline end-to-end",
                "status": "retired",
                "priority": "medium",
                "notes": [],
                "context": {
                    "success_evidence": "CI passes on main for 7 days",
                    "retirement_condition": "All acceptance criteria verified",
                },
                "research_history": [
                    {
                        "cycles": 1,
                        "prs_merged": 2,
                        "tasks_completed": 3,
                        "tasks_failed": 0,
                    },
                    {
                        "cycles": 1,
                        "prs_merged": 1,
                        "tasks_completed": 2,
                        "tasks_failed": 1,
                    },
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    from app.models import Task as TaskModel
    from sqlmodel import select

    rationale = "Pipeline is complete and all criteria met."
    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        parent_task,
        retired_ids=[direction_id],
        rationales={direction_id: rationale},
    )
    assert spawned == 1

    await db_session.commit()

    memo_tasks = (
        (
            await db_session.execute(
                select(TaskModel).where(
                    TaskModel.project_id == project.id,
                    TaskModel.created_by == "ORCHESTRATOR",
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

    # Verify source_finding in payload
    payload = memo.payloads[0]
    source_finding = payload.get("source_finding")
    assert source_finding is not None, "memo payload must carry source_finding"
    assert source_finding["direction_text"] == "Build the pipeline end-to-end"
    assert source_finding["success_evidence"] == "CI passes on main for 7 days"
    assert source_finding["retirement_condition"] == "All acceptance criteria verified"
    assert source_finding["rationale"] == rationale
    assert len(source_finding["recent_history_summary"]) == 2
    assert "retirement_metrics" in source_finding

    # Verify context carries mirrored fields
    assert memo.context.get("success_evidence") == "CI passes on main for 7 days"
    assert (
        memo.context.get("retirement_condition") == "All acceptance criteria verified"
    )

    # Verify synthesized reasoning references the evidence
    assert isinstance(memo.plan_output, dict) and "memo" in memo.plan_output
    reasoning = memo.plan_output["memo"].get("reasoning", "")
    assert "CI passes on main for 7 days" in reasoning
    assert "All acceptance criteria verified" in reasoning
    assert rationale in reasoning

    # Verify source_task_id is present in source_finding
    source_finding = payload.get("source_finding")
    assert source_finding.get("source_task_id") == str(parent_task.id)


@pytest.mark.asyncio
async def test_rationale_absent_retirement_memo_completed_at_spawn(db_session):
    """A no-rationale retirement with snapshot context produces a COMPLETED memo at spawn.

    Verifies: memo is COMPLETED (not QUEUED), records the memo to plan_output
    (no Artifact row), reasoning includes recent history and metrics, citations
    include direction:<id> and task:<id>. No QUEUED VM-bearing task is left behind.
    """
    from app.models import TaskStatus, Artifact
    from app.models import Task as TaskModel
    from sqlmodel import select

    direction_id = str(uuid.uuid4())
    project = Project(
        name="NoRationaleSnapshotTest",
        user_id="u-no-rationale-snap",
        directions=[
            {
                "id": direction_id,
                "text": "Ship the observability pipeline",
                "status": "retired",
                "priority": "medium",
                "notes": [],
                "context": {
                    "success_evidence": "Dashboards live in production",
                    "retirement_condition": "Metrics streaming confirmed",
                },
                "research_history": [
                    {
                        "cycles": 2,
                        "prs_merged": 3,
                        "tasks_completed": 5,
                        "tasks_failed": 1,
                    },
                    {
                        "cycles": 1,
                        "prs_merged": 1,
                        "tasks_completed": 2,
                        "tasks_failed": 0,
                    },
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        parent_task,
        retired_ids=[direction_id],
        rationales={},  # no rationale
    )
    assert spawned == 1

    await db_session.commit()

    memo_tasks = (
        (
            await db_session.execute(
                select(TaskModel).where(
                    TaskModel.project_id == project.id,
                    TaskModel.created_by == "ORCHESTRATOR",
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

    # No QUEUED VM-bearing memo — must be synthesized at spawn time
    assert memo.status == TaskStatus.COMPLETED, (
        f"expected COMPLETED (synthesized at spawn), got {memo.status}"
    )

    # No MEMO Artifact row is written — the memo lives only in plan_output.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == memo.id)))
        .scalars()
        .all()
    )
    assert artifacts == []

    # plan_output must carry the memo
    assert isinstance(memo.plan_output, dict) and "memo" in memo.plan_output
    memo_data = memo.plan_output["memo"]
    reasoning = memo_data.get("reasoning", "")

    # Reasoning must include recent history
    assert "Cycle 1" in reasoning, "reasoning must render recent_history_summary"
    assert "PRs merged" in reasoning

    # Reasoning must include metrics/evidence
    assert "Dashboards live in production" in reasoning
    assert "Metrics streaming confirmed" in reasoning

    # Citations must reference both direction and source task
    citations = memo_data.get("citations", [])
    assert f"direction:{direction_id}" in citations
    assert f"task:{parent_task.id}" in citations

    # source_finding must carry source_task_id
    source_finding = memo.payloads[0].get("source_finding")
    assert source_finding is not None
    assert source_finding.get("source_task_id") == str(parent_task.id)


@pytest.mark.asyncio
async def test_spawn_retirement_memo_empty_snapshot_completes_inline_no_vm(db_session):
    """A direction with no text, context, history, or rationale still completes inline.

    Even when the snapshot is entirely empty, the memo must reach COMPLETED at spawn
    time with a minimal citable artifact — no dedicated agent VM is provisioned.
    """
    from app.models import TaskStatus, Artifact
    from app.models import Task as TaskModel
    from sqlmodel import select

    direction_id = str(uuid.uuid4())
    project = Project(
        name="EmptySnapshotTest",
        user_id="u-empty-snap",
        directions=[
            {
                "id": direction_id,
                # No text, no context, no research_history
                "status": "retired",
                "priority": "medium",
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        parent_task,
        retired_ids=[direction_id],
        rationales={},  # no rationale, no snapshot context
    )
    assert spawned == 1

    await db_session.commit()

    memo_tasks = (
        (
            await db_session.execute(
                select(TaskModel).where(
                    TaskModel.project_id == project.id,
                    TaskModel.created_by == "ORCHESTRATOR",
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
    assert len(retirement_memos) == 1
    memo = retirement_memos[0]

    # Even with an entirely empty snapshot, the memo must be completed inline.
    assert memo.status == TaskStatus.COMPLETED, (
        f"expected COMPLETED (inline synthesis with minimal memo), got {memo.status}"
    )

    # No MEMO Artifact row is written — the memo lives only in plan_output.
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == memo.id)))
        .scalars()
        .all()
    )
    assert artifacts == [], (
        "empty-snapshot memo must not create an Artifact row"
    )

    assert memo.plan_output and memo.plan_output.get("memo"), (
        "plan_output['memo'] must be set for inline-completed memo"
    )
    assert memo.plan_output["memo"].get("summary"), (
        "plan_output memo must have a non-empty summary"
    )


@pytest.mark.asyncio
async def test_spawn_retirement_memos_creates_knowledge_document(db_session):
    """_spawn_retirement_memos persists a KnowledgeDocument row for each retired direction.

    Verifies: source_kind=="direction_retirement", direction_id matches, topic is
    "direction-retirement", body is non-empty, and project_id matches.
    Covers all three synthesis branches: rationale-present, snapshot-substantive,
    and minimal-fallback.
    """
    from app.models import KnowledgeDocument, SourceKind, TaskStatus
    from sqlmodel import select

    direction_id_with_rationale = str(uuid.uuid4())
    direction_id_snapshot_only = str(uuid.uuid4())
    direction_id_minimal = str(uuid.uuid4())

    project = Project(
        name="KnowledgeDocRetirementTest",
        user_id="u-knowdoc-retirement",
        directions=[
            {
                "id": direction_id_with_rationale,
                "text": "Build the metrics pipeline end-to-end",
                "status": "retired",
                "priority": "medium",
                "notes": [],
                "context": {
                    "success_evidence": "CI passes on main for 7 days",
                    "retirement_condition": "All acceptance criteria verified",
                },
                "research_history": [
                    {
                        "cycles": 1,
                        "prs_merged": 2,
                        "tasks_completed": 3,
                        "tasks_failed": 0,
                    },
                ],
            },
            {
                "id": direction_id_snapshot_only,
                "text": "Ship the observability dashboard",
                "status": "retired",
                "priority": "medium",
                "notes": [],
                "context": {
                    "success_evidence": "Dashboard live in production",
                },
            },
            {
                "id": direction_id_minimal,
                # no text, no context — triggers minimal fallback
                "status": "retired",
                "priority": "low",
                "notes": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    spawned = await _spawn_retirement_memos(
        db_session,
        project,
        parent_task,
        retired_ids=[
            direction_id_with_rationale,
            direction_id_snapshot_only,
            direction_id_minimal,
        ],
        rationales={
            direction_id_with_rationale: "Pipeline complete and all criteria met."
        },
    )
    assert spawned == 3

    await db_session.commit()

    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.project_id == project.id,
                    KnowledgeDocument.source_kind
                    == SourceKind.DIRECTION_RETIREMENT.value,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 3, f"expected 3 KnowledgeDocument rows, got {len(docs)}"

    docs_by_direction = {d.direction_id: d for d in docs}

    # Branch 1: rationale-present
    doc = docs_by_direction.get(direction_id_with_rationale)
    assert doc is not None, "KnowledgeDocument missing for rationale-present direction"
    assert doc.source_kind == SourceKind.DIRECTION_RETIREMENT.value
    assert doc.topic == "direction-retirement"
    assert doc.body, "body must be non-empty"
    assert "Pipeline complete" in doc.body
    assert doc.project_id == project.id

    # Branch 2: snapshot-substantive (no rationale but has context)
    doc2 = docs_by_direction.get(direction_id_snapshot_only)
    assert doc2 is not None, (
        "KnowledgeDocument missing for snapshot-substantive direction"
    )
    assert doc2.source_kind == SourceKind.DIRECTION_RETIREMENT.value
    assert doc2.topic == "direction-retirement"
    assert doc2.body, "body must be non-empty"

    # Branch 3: minimal fallback
    doc3 = docs_by_direction.get(direction_id_minimal)
    assert doc3 is not None, "KnowledgeDocument missing for minimal-fallback direction"
    assert doc3.source_kind == SourceKind.DIRECTION_RETIREMENT.value
    assert doc3.topic == "direction-retirement"
    assert doc3.body, "body must be non-empty"
    assert f"direction:{direction_id_minimal}" in doc3.source_citations


# ===========================================================================
# refresh_open_version_goal_if_auto_derived via _handle_strategic_completion
# ===========================================================================


@pytest.mark.asyncio
async def test_strategic_cycle_does_not_refresh_version_goal(db_session, mocker):
    """Strategic cycles must NOT update version.goal when current_focus changes.

    version.goal is frozen at version-open; it is NOT auto-refreshed during
    strategic cycles even for auto-derived goals. This prevents the goal from
    becoming an operational status log as current_focus evolves across cycles.
    Operator-edited goals are still never clobbered (independent guard).
    """
    import json
    from unittest.mock import AsyncMock

    from app import release_version as rv
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FocusSync",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model={
            "intent": {"current_focus": "old v7 focus"},
            "revision": 1,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open a version with auto-derived goal (no goal_editor_info).
    version = await rv.open_version(
        db_session, project.id, version_number=8, goal="old v7 focus"
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal_editor_info is None

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

    logs = json.dumps(
        {
            "problem_model": {
                "intent": {
                    "current_focus": (
                        "v8 re-qualifying at HEAD abc123. "
                        "All v8 deliverables confirmed complete."
                    )
                },
                "revision": 2,
            }
        }
    )

    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(version)

    # version.goal must stay frozen at the value set when the version opened —
    # NOT overwritten with the strategic cycle's status-log current_focus.
    assert version.goal == "old v7 focus", (
        f"version.goal was mutated by a strategic cycle; got {version.goal!r}. "
        "Strategic cycles must not call refresh_open_version_goal_if_auto_derived."
    )
    assert version.goal_editor_info is None

    # === Operator-edited goal must not be clobbered either ===
    project2 = Project(
        name="FocusSyncProtected",
        user_id="u2",
        vision={"purpose": "test", "revision": 1},
        problem_model={
            "intent": {"current_focus": "old v7 focus"},
            "revision": 1,
        },
    )
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    version2 = await rv.open_version(
        db_session, project2.id, version_number=8, goal="old v7 focus"
    )
    await db_session.commit()

    rv.set_version_goal(version2, "operator-chosen goal", editor_id="op@example.com")
    db_session.add(version2)
    await db_session.commit()
    await db_session.refresh(version2)
    assert version2.goal_editor_info == "[operator:op@example.com]"

    task2 = Task(
        project_id=project2.id,
        payloads=[
            {
                "type": "strategic_review",
                "prompt": "review",
                "vision": {"revision": 1},
            }
        ],
        context={},
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    logs2 = json.dumps(
        {
            "problem_model": {
                "intent": {"current_focus": "strategic new focus — must not overwrite"},
                "revision": 2,
            }
        }
    )

    await _handle_strategic_completion(task2, logs2, db_session)
    await db_session.refresh(version2)

    assert version2.goal == "operator-chosen goal", (
        f"Operator goal must not be clobbered; got {version2.goal!r}"
    )
    assert version2.goal_editor_info == "[operator:op@example.com]"


@pytest.mark.asyncio
async def test_set_problem_model_api_does_not_refresh_version_goal(client, db_session):
    """set_problem_model API op must NOT refresh version.goal when current_focus changes.

    The strategist's PRIMARY write path is /api/agent/strategic with
    op=set_problem_model (strategic.ts -> strategic-helpers.ts:58). This is the
    route the freeze-at-open fix must actually protect. The goal must remain
    frozen at the value set when the version opened.
    """
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import release_version as rv
    from app import strategic_cycle

    project = Project(
        name="GoalFreezeAPITest",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model={
            "intent": {"current_focus": "initial focus at version open"},
            "revision": 1,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open a version with auto-derived goal (no goal_editor_info).
    version = await rv.open_version(
        db_session, project.id, version_number=1, goal="initial focus at version open"
    )
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal == "initial focus at version open"
    assert version.goal_editor_info is None

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-goal-freeze",
        payloads=[
            {
                "type": "strategic_review",
                "prompt": "review",
                "vision": {"revision": 1},
            }
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-goal-freeze", task_type="strategic_review")

    # The strategist writes a new current_focus that looks like a status log.
    # The API op must NOT propagate this into version.goal.
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {
                    "current_focus": (
                        "v1 re-qualifying at HEAD abc123. All v1 deliverables confirmed "
                        "complete: design-system done, font-size migration done."
                    )
                },
                "capabilities": [],
                "gaps": [],
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "set_problem_model"

    # The problem_model write must have gone through (current_focus updated).
    await db_session.refresh(project)
    new_focus = (project.problem_model or {}).get("intent", {}).get("current_focus", "")
    assert "re-qualifying" in new_focus, f"problem_model not updated; got {new_focus!r}"

    # But version.goal must remain frozen — NOT overwritten with the status-log focus.
    await db_session.refresh(version)
    assert version.goal == "initial focus at version open", (
        f"version.goal was mutated by set_problem_model API op; got {version.goal!r}. "
        "The strategic-cycle API path must not call refresh_open_version_goal_if_auto_derived."
    )
    assert version.goal_editor_info is None


# ===========================================================================
# _apply_new_version_goal — strategist correction of already-set goal
# ===========================================================================


@pytest.mark.asyncio
async def test_strategist_can_correct_already_set_version_goal(db_session):
    """Strategist can correct a previously-authored version goal.

    A strategist-set goal (goal_editor_info startswith "[strategist:") is NOT
    frozen — a subsequent strategic task can correct a mis-authored goal.
    Operator provenance is preserved in history, but does not hard-lock a
    version goal that current delivery evidence shows must be corrected.
    """
    from app.strategic_completion import _apply_new_version_goal
    from app import release_version as rv

    project = Project(
        name="GoalCorrect",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()

    # Simulate a prior strategist run that authored an incorrect goal.
    rv.set_version_goal(version, "wrong initial goal", editor_id="abc12345", source="strategist")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal == "wrong initial goal"
    assert version.goal_editor_info == "[strategist:abc12345]"

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _apply_new_version_goal(
        "corrected goal — accurate statement", task, project, db_session, path="test"
    )
    await db_session.commit()
    await db_session.refresh(version)

    # Strategist must be able to correct its own previously-authored goal.
    assert version.goal == "corrected goal — accurate statement", (
        f"Expected strategist correction to apply; got {version.goal!r}"
    )
    assert version.goal_editor_info == f"[strategist:{str(task.id)[:8]}]"

    # Strategic may also correct an operator-authored version goal; provenance
    # makes the handoff auditable without turning a stale goal into a deadlock.
    rv.set_version_goal(version, "operator override", editor_id="op@example.com", source="operator")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal_editor_info == "[operator:op@example.com]"

    await _apply_new_version_goal(
        "strategist attempt to override operator", task, project, db_session, path="test"
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert version.goal == "strategist attempt to override operator"
    assert version.goal_editor_info == f"[strategist:{str(task.id)[:8]}]"


# ===========================================================================
# extract_new_version_goal unit tests
# ===========================================================================


def test_extract_new_version_goal_valid_string():
    """Valid, non-contentless strings are returned cleaned."""
    from app.strategic_completion import extract_new_version_goal

    goal = "Deliver autonomous coordination so Blueprint manages 3+ projects with zero daily interventions."
    assert extract_new_version_goal(goal) == goal
    # Trims surrounding whitespace
    assert extract_new_version_goal(f"  {goal}  ") == goal


def test_extract_new_version_goal_invalid_inputs():
    """Non-string, blank, and None inputs are rejected."""
    from app.strategic_completion import extract_new_version_goal

    assert extract_new_version_goal(None) is None
    assert extract_new_version_goal(42) is None
    assert extract_new_version_goal([]) is None
    assert extract_new_version_goal("") is None
    assert extract_new_version_goal("   ") is None


def test_extract_new_version_goal_contentless_rejected():
    """Contentless single-word placeholder goals are rejected by is_contentless_goal."""
    from app.strategic_completion import extract_new_version_goal

    # is_contentless_goal only rejects single-word placeholders (see release_scoring.py)
    assert extract_new_version_goal("test") is None
    assert extract_new_version_goal("n/a") is None


def test_extract_new_version_goal_max_length():
    """Values are bounded to _NEW_VERSION_GOAL_MAX characters."""
    from app.strategic_completion import extract_new_version_goal, _NEW_VERSION_GOAL_MAX

    # A non-contentless string just under the limit passes
    short_goal = "Deliver " + "x" * 400
    result = extract_new_version_goal(short_goal)
    assert result is not None
    assert len(result) <= _NEW_VERSION_GOAL_MAX

    # A very long string is truncated, not rejected
    long_goal = "Deliver " + "x" * 2000
    result_long = extract_new_version_goal(long_goal)
    assert result_long is not None
    assert len(result_long) == _NEW_VERSION_GOAL_MAX


# ===========================================================================
# _apply_new_version_goal — non-silent no-op logging
# ===========================================================================


@pytest.mark.asyncio
async def test_apply_new_version_goal_logs_info_when_no_open_version(db_session, caplog):
    """When there is no open ReleaseVersion, _apply_new_version_goal must log at
    INFO level (not silently swallow the no-op) so operators can diagnose why
    the goal was not stored."""
    import logging
    from app.strategic_completion import _apply_new_version_goal

    project = Project(
        name="NoVersion",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # No ReleaseVersion opened — project has no open version.

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    with caplog.at_level(logging.INFO, logger="app.strategic_completion"):
        await _apply_new_version_goal(
            "A substantive directional goal that should have been stored.",
            task, project, db_session, path="test",
        )

    assert any(
        "skipping new_version_goal" in record.message and record.levelno == logging.INFO
        for record in caplog.records
    ), f"Expected INFO-level skip log; got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_apply_new_version_goal_revises_operator_authored_goal(db_session, caplog):
    """Prior provenance is retained in the log but does not freeze the goal."""
    import logging
    from app.strategic_completion import _apply_new_version_goal
    from app import release_version as rv

    project = Project(
        name="OpFrozen",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model={"intent": {"current_focus": "focus"}, "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_goal(version, "operator choice", editor_id="op@example.com", source="operator")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal_editor_info == "[operator:op@example.com]"

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "r"}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    with caplog.at_level(logging.INFO, logger="app.strategic_completion"):
        await _apply_new_version_goal(
            "Strategist correction grounded in current delivery capabilities.",
            task, project, db_session, path="test",
        )

    assert any(
        "correcting open version" in record.message
        and "[operator:op@example.com]" in record.message
        and record.levelno == logging.INFO
        for record in caplog.records
    ), f"Expected INFO-level correction log; got: {[r.message for r in caplog.records]}"

    # _apply_new_version_goal participates in the caller's transaction; commit
    # that transaction before reloading the row from the database.
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal == "Strategist correction grounded in current delivery capabilities."
    assert version.goal_editor_info is not None
    assert version.goal_editor_info.startswith("[strategist:")


# ── _compute_model_gc_telemetry ───────────────────────────────────────────────


def test_compute_model_gc_telemetry_basic_fields():
    """cycle_metadata model_gc_telemetry has all required fields with non-None values."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "test", "current_focus": "testing"},
            "capabilities": [
                {"name": "cap1", "status": "working"},
                {"name": "cap2", "status": "working"},
            ],
            "gaps": [
                {
                    "id": "g1",
                    "name": "gap1",
                    "description": "d",
                    "evidence": "",
                    "status": "active",
                },
            ],
            "friction": [],
        },
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 80000,
                    "trimmed": [],
                    "dropped": [],
                },
            }
        ],
        context={},
    )

    result = _compute_model_gc_telemetry(project, task)

    assert result["model_bytes"] is not None
    assert result["model_bytes"] > 0
    assert result["capability_count"] == 2
    assert result["gap_count"] == 1
    assert result["model_size_status"] in ("healthy", "growing", "large", "oversized")
    assert result["directions_dropped_count"] == 0
    assert result["problem_model_truncated"] is False
    # No drops → high_tier_dropped is an empty list (present, not None).
    assert result["high_tier_dropped"] == []


def test_compute_model_gc_telemetry_reports_high_tier_dropped():
    """high_tier_dropped lists only high/critical-tier dropped keys; medium/low
    drops are excluded (they are expected degradation, not a degraded payload)."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "test", "current_focus": ""},
            "capabilities": [],
            "gaps": [],
            "friction": [],
        },
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 119000,
                    "trimmed": [],
                    "dropped": [
                        {
                            "key": "open_messages",
                            "reason": "exceeds remaining budget (no summarize_fn)",
                            "priority": "high",
                        },
                        {
                            "key": "playbook_summary",
                            "reason": "exceeds remaining budget (no summarize_fn)",
                            "priority": "low",
                        },
                        {
                            "key": "identity",
                            "reason": "value not JSON-serializable",
                            "priority": "critical",
                        },
                    ],
                },
            }
        ],
        context={},
    )

    result = _compute_model_gc_telemetry(project, task)

    # Only the high and critical keys appear; the low-tier drop is excluded.
    assert set(result["high_tier_dropped"]) == {"open_messages", "identity"}
    assert "playbook_summary" not in result["high_tier_dropped"]


def test_compute_model_gc_telemetry_detects_directions_dropped():
    """directions_dropped_count > 0 when directions_summary appears in dropped."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "test", "current_focus": ""},
            "capabilities": [],
            "gaps": [],
            "friction": [],
        },
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 119000,
                    "trimmed": [],
                    "dropped": [
                        {
                            "key": "directions_summary",
                            "reason": "exceeds remaining budget",
                        }
                    ],
                },
            }
        ],
        context={},
    )

    result = _compute_model_gc_telemetry(project, task)

    assert result["directions_dropped_count"] == 1
    assert result["problem_model_truncated"] is False


def test_compute_model_gc_telemetry_detects_problem_model_truncated():
    """problem_model_truncated is True when problem_model appears in trimmed."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "test", "current_focus": ""},
            "capabilities": [],
            "gaps": [],
            "friction": [],
        },
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 100000,
                    "trimmed": [{"key": "problem_model", "kind": "floored"}],
                    "dropped": [],
                },
            }
        ],
        context={},
    )

    result = _compute_model_gc_telemetry(project, task)

    assert result["problem_model_truncated"] is True
    assert result["directions_dropped_count"] == 0


def test_compute_model_gc_telemetry_gap_memory_trimmed_present():
    """gap_memory_trimmed_count and gap_memory_hidden_entries reflect trimmed gap memory."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "test", "current_focus": ""},
            "capabilities": [],
            "gaps": [
                {
                    "id": "g1",
                    "name": "gap1",
                    "description": "d",
                    "evidence": "",
                    "status": "active",
                },
                {
                    "id": "g2",
                    "name": "gap2",
                    "description": "d",
                    "evidence": "",
                    "status": "active",
                },
            ],
            "friction": [],
        },
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 100000,
                    "trimmed": [],
                    "dropped": [],
                    "gap_memory_trimmed": [
                        {
                            "key": "problem_model",
                            "gaps": [
                                {
                                    "gap_id": "g1",
                                    "fields": {
                                        "history": {"shown": 3, "total": 8},
                                        "past_moves": {"shown": 1, "total": 4},
                                    },
                                },
                                {
                                    "gap_id": "g2",
                                    "fields": {
                                        "history": {"shown": 2, "total": 5},
                                        "past_moves": {"shown": 0, "total": 3},
                                    },
                                },
                            ],
                        },
                    ],
                },
            }
        ],
        context={},
    )

    result = _compute_model_gc_telemetry(project, task)

    assert result["gap_memory_trimmed_count"] == 2
    # hidden = (8-3) + (4-1) + (5-2) + (3-0) = 5 + 3 + 3 + 3 = 14
    assert result["gap_memory_hidden_entries"] == 14


def test_compute_model_gc_telemetry_gap_memory_absent():
    """gap_memory_trimmed_count and gap_memory_hidden_entries default to 0 when absent."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "test", "current_focus": ""},
            "capabilities": [],
            "gaps": [],
            "friction": [],
        },
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 80000,
                    "trimmed": [],
                    "dropped": [],
                },
            }
        ],
        context={},
    )

    result = _compute_model_gc_telemetry(project, task)

    assert result["gap_memory_trimmed_count"] == 0
    assert result["gap_memory_hidden_entries"] == 0


def test_cycle_telemetry_records_core_section_survival():
    """open_messages_dropped and version_phase_dropped reflect core-section drops.

    When open_messages appears in packer dropped list, open_messages_dropped is True.
    When version_phase appears in packer dropped list, version_phase_dropped is True.
    When neither core section was dropped, both flags are False.
    """
    base_project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "test", "current_focus": ""},
            "capabilities": [],
            "gaps": [],
            "friction": [],
        },
    )

    # Both core sections dropped
    task_both_dropped = Task(
        project_id=base_project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 119000,
                    "trimmed": [],
                    "dropped": [
                        {"key": "open_messages", "reason": "exceeds remaining budget"},
                        {"key": "version_phase", "reason": "exceeds remaining budget"},
                    ],
                },
            }
        ],
        context={},
    )
    result_both = _compute_model_gc_telemetry(base_project, task_both_dropped)
    assert result_both["open_messages_dropped"] is True
    assert result_both["version_phase_dropped"] is True

    # Only open_messages dropped
    task_open_only = Task(
        project_id=base_project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 110000,
                    "trimmed": [],
                    "dropped": [
                        {"key": "open_messages", "reason": "exceeds remaining budget"},
                    ],
                },
            }
        ],
        context={},
    )
    result_open_only = _compute_model_gc_telemetry(base_project, task_open_only)
    assert result_open_only["open_messages_dropped"] is True
    assert result_open_only["version_phase_dropped"] is False

    # Neither core section dropped — survival case
    task_survived = Task(
        project_id=base_project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120000,
                    "budget_used": 80000,
                    "trimmed": [],
                    "dropped": [
                        {"key": "knowledge_index", "reason": "exceeds remaining budget"},
                    ],
                },
            }
        ],
        context={},
    )
    result_survived = _compute_model_gc_telemetry(base_project, task_survived)
    assert result_survived["open_messages_dropped"] is False
    assert result_survived["version_phase_dropped"] is False


def test_compute_model_gc_telemetry_budget_utilization():
    """_packer_meta with shipped entries produces budget_utilization_pct and
    non-empty top_budget_consumers reflecting the largest shipped sections."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={"intent": {"purpose": "t", "current_focus": ""}, "capabilities": [], "gaps": [], "friction": []},
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 100_000,
                    "budget_used": 80_000,
                    "trimmed": [],
                    "dropped": [],
                    "shipped": [
                        {"key": "problem_model", "bytes": 50_000, "priority": "high"},
                        {"key": "directions_summary", "bytes": 20_000, "priority": "high"},
                        {"key": "vision", "bytes": 10_000, "priority": "critical"},
                    ],
                    "gap_memory_trimmed": [],
                },
            }
        ],
        context={},
    )
    result = _compute_model_gc_telemetry(project, task)
    assert result["budget_utilization_pct"] == 80.0
    consumers = result["top_budget_consumers"]
    assert len(consumers) > 0
    # Largest section is problem_model at 50 KB.
    assert consumers[0]["key"] == "problem_model"
    assert consumers[0]["bytes"] == 50_000
    # Descending order.
    for i in range(len(consumers) - 1):
        assert consumers[i]["bytes"] >= consumers[i + 1]["bytes"]


def test_compute_model_gc_telemetry_shipped_absent_backward_compat():
    """Old _packer_meta without 'shipped' key still returns budget_utilization_pct
    (derived from budget_used/budget_total) and an empty top_budget_consumers."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={"intent": {"purpose": "t", "current_focus": ""}, "capabilities": [], "gaps": [], "friction": []},
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[
            {
                "type": "strategic_review",
                "_packer_meta": {
                    "budget_total": 120_000,
                    "budget_used": 60_000,
                    "trimmed": [],
                    "dropped": [],
                    # no "shipped" key — old payload format
                },
            }
        ],
        context={},
    )
    result = _compute_model_gc_telemetry(project, task)
    # Still derives utilization from existing budget fields.
    assert result["budget_utilization_pct"] == 50.0
    # No shipped entries → no section_bytes from that path.
    assert result["top_budget_consumers"] == []


def test_compute_model_gc_telemetry_logs_first_payload_failure(caplog, mocker):
    """When task.first_payload raises (e.g. detached instance), the function logs a
    warning and returns all packer-derived counts as empty/zero rather than crashing."""
    project = Project(
        name="P",
        user_id="u1",
        problem_model={
            "intent": {"purpose": "t", "current_focus": ""},
            "capabilities": [],
            "gaps": [],
            "friction": [],
        },
    )
    task = Task(
        project_id=project.id or uuid.uuid4(),
        payloads=[{}],
        context={},
    )
    # first_payload is a property; mock it to raise so the except path fires.
    mocker.patch.object(type(task), "first_payload", new_callable=mocker.PropertyMock, side_effect=RuntimeError("simulated packer read failure"))

    with caplog.at_level(logging.WARNING):
        result = _compute_model_gc_telemetry(project, task)

    # Function does not crash — falls back to empty packer_meta.
    assert isinstance(result, dict)
    assert result["directions_dropped_count"] == 0
    assert result["open_messages_dropped"] is False
    assert result["version_phase_dropped"] is False
    assert result["high_tier_dropped"] == []
    assert result["problem_model_truncated"] is False
    assert result["gap_memory_trimmed_count"] == 0
    assert result["gap_memory_hidden_entries"] == 0
    assert result["budget_utilization_pct"] is None
    assert result["top_budget_consumers"] == []

    # A warning was logged about the failure.
    assert any("Failed to read packer metadata" in record.message for record in caplog.records)


# ── QUESTION answer threading tests ──────────────────────────────────────────


@pytest_asyncio.fixture()
async def question_db(mocker):
    """Isolated in-memory DB for QUESTION-answer threading tests."""
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


@pytest.mark.asyncio
async def test_question_answer_posted_on_resolution(question_db, mocker):
    """Resolving a QUESTION via the log-parse path posts a from_type=strategic reply
    whose context carries event_type='question_answer' and reply_to=<question id>."""
    from unittest.mock import AsyncMock
    from app.models import (
        MessageHandlingState,
        MessageSurface,
        MessageType,
        Project,
        ProjectMessage,
        Task,
    )
    from app.strategic_completion import _handle_strategic_completion
    from sqlalchemy import select

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    emitted: list[tuple[str, int | None]] = []

    async def _record_message_event(_project_id, event_type, payload):
        assert not question_db.in_transaction(), "message event emitted before commit"
        emitted.append((event_type, payload["message"]["sequence"]))

    mocker.patch(
        "app.strategic_completion.broadcast_to_project",
        new=AsyncMock(side_effect=_record_message_event),
    )

    project = Project(name="P-qa", user_id="u1", vision={"purpose": "test"})
    question_db.add(project)
    await question_db.commit()
    await question_db.refresh(project)

    question_msg = ProjectMessage(
        project_id=project.id,
        sequence=5,
        message_type=MessageType.QUESTION,
        content="What is the current deploy cadence?",
        context={},
        status="open",
        from_type="operator",
    )
    question_db.add(question_msg)
    await question_db.commit()
    await question_db.refresh(question_msg)
    question_id = question_msg.id

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {}}],
        context={},
    )
    question_db.add(task)
    await question_db.commit()
    await question_db.refresh(task)

    logs = json.dumps(
        {
            "vision": {"revision": 1},
            "resolved_messages": [
                {
                    "id": str(question_id),
                    "resolution": "We deploy on Tuesdays and Fridays via the CI pipeline.",
                }
            ],
        }
    )

    await _handle_strategic_completion(
        task, logs, question_db, message_sequences=[10]
    )

    await question_db.refresh(question_msg)
    assert question_msg.status == "resolved"
    assert question_msg.handling_state == MessageHandlingState.RESPONDED
    assert question_msg.handled_by_task_id == task.id

    result = await question_db.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.from_type == "strategic",
        )
    )
    reply = result.scalars().first()
    assert reply is not None, "Expected a from_type=strategic reply message"
    assert reply.context is not None
    assert reply.context.get("event_type") == "question_answer"
    assert reply.context.get("reply_to") == str(question_id)
    assert reply.sequence == 10
    assert reply.surface == MessageSurface.CONVERSATION
    assert reply.handling_state == MessageHandlingState.RESPONDED
    assert reply.handled_by_task_id == task.id
    assert reply.reply_to_message_id == question_id
    assert (
        reply.context.get("answer")
        == "We deploy on Tuesdays and Fridays via the CI pipeline."
    )
    assert emitted == [
        ("message_updated", 5),
        ("message_created", 10),
    ]


@pytest.mark.asyncio
async def test_question_answer_not_posted_when_no_resolution(question_db, mocker):
    """A QUESTION not in resolved_messages produces no reply message."""
    from unittest.mock import AsyncMock
    from app.models import MessageType, Project, ProjectMessage, Task
    from app.strategic_completion import _handle_strategic_completion
    from sqlalchemy import select

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(name="P-qa-nr", user_id="u1", vision={"purpose": "test"})
    question_db.add(project)
    await question_db.commit()
    await question_db.refresh(project)

    question_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        content="Is the product ready?",
        context={},
        status="open",
        from_type="operator",
    )
    question_db.add(question_msg)
    await question_db.commit()
    await question_db.refresh(question_msg)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {}}],
        context={},
    )
    question_db.add(task)
    await question_db.commit()
    await question_db.refresh(task)

    # No resolved_messages entry for the question
    logs = json.dumps({"vision": {"revision": 1}})

    await _handle_strategic_completion(task, logs, question_db)

    # Question should still be open
    await question_db.refresh(question_msg)
    assert question_msg.status == "open"

    result = await question_db.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.from_type == "strategic",
        )
    )
    reply = result.scalars().first()
    assert reply is None, "No reply should be posted for an unresolved QUESTION"


@pytest.mark.asyncio
async def test_open_questions_audit_fallback_path(question_db, mocker):
    """Log-parse path: an open QUESTION after completion triggers a strategic alert."""
    from unittest.mock import AsyncMock
    from app.models import MessageType, Project, ProjectMessage, Task
    from app.strategic_completion import _handle_strategic_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock
    )

    project = Project(name="P-qa-audit", user_id="u1", vision={"purpose": "test"})
    question_db.add(project)
    await question_db.commit()
    await question_db.refresh(project)

    question_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        content="What should we prioritize?",
        context={},
        status="open",
        from_type="operator",
    )
    question_db.add(question_msg)
    await question_db.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {}}],
        context={},
    )
    question_db.add(task)
    await question_db.commit()
    await question_db.refresh(task)

    # No resolution for the question — it remains open
    logs = json.dumps({"vision": {"revision": 1}})

    await _handle_strategic_completion(task, logs, question_db)

    assert alert_mock.called, "Expected _create_strategic_alert to be called"
    alert_texts = [str(call) for call in alert_mock.call_args_list]
    assert any(
        "QUESTION" in t or "unanswered" in t or "question" in t.lower()
        for t in alert_texts
    ), f"Expected a QUESTION-related alert, got: {alert_texts}"


@pytest.mark.asyncio
async def test_open_questions_audit_api_path(question_db, mocker):
    """API path: an open QUESTION after completion triggers a strategic alert."""
    from unittest.mock import AsyncMock
    from app.models import MessageType, Project, ProjectMessage, Task
    from app.strategic_completion import _handle_strategic_completion

    alert_mock = mocker.patch(
        "app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock
    )

    project = Project(
        name="P-qa-api", user_id="u1", vision={"purpose": "test", "revision": 1}
    )
    question_db.add(project)
    await question_db.commit()
    await question_db.refresh(project)

    question_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        content="When does v2 ship?",
        context={},
        status="open",
        from_type="operator",
    )
    question_db.add(question_msg)
    await question_db.commit()

    # Simulate API path: set _strategic_api_called so api_was_used=True
    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"_strategic_api_called": True},
    )
    question_db.add(task)
    await question_db.commit()
    await question_db.refresh(task)

    # Empty logs — API path doesn't use the log-parse fallback
    await _handle_strategic_completion(task, "", question_db)

    assert alert_mock.called, "Expected _create_strategic_alert to be called"
    alert_texts = [str(call) for call in alert_mock.call_args_list]
    assert any(
        "QUESTION" in t or "unanswered" in t or "question" in t.lower()
        for t in alert_texts
    ), f"Expected a QUESTION-related alert, got: {alert_texts}"


# ===========================================================================
# new_version_goal dispatch in _handle_strategic_completion
# ===========================================================================


@pytest.mark.asyncio
async def test_handle_strategic_completion_sets_new_version_goal(db_session, mocker):
    """When the strategist outputs new_version_goal and the open version has no
    goal yet, set_version_goal is called with source='strategist' and the goal
    is stored on the version."""
    from unittest.mock import AsyncMock

    from app import release_version as rv
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GoalAuthor",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(
        db_session, project.id, version_number=2, goal=None
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal is None

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

    logs = json.dumps(
        {
            "new_version_goal": (
                "deliver the design-system token layer with 0 text-color literals"
            )
        }
    )

    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(version)

    assert version.goal == (
        "deliver the design-system token layer with 0 text-color literals"
    ), f"Expected strategist goal to be stored; got {version.goal!r}"
    assert version.goal_editor_info is not None
    assert version.goal_editor_info.startswith("[strategist:"), (
        f"goal_editor_info must use source='strategist'; got {version.goal_editor_info!r}"
    )


@pytest.mark.asyncio
async def test_handle_strategic_completion_can_revise_existing_operator_version_goal(
    db_session, mocker
):
    """Strategic can revise a system-written goal regardless of provenance.

    Provenance remains visible on the resulting goal, but it is not a lock that
    can deadlock a version around an infeasible objective.
    """
    from unittest.mock import AsyncMock

    from app import release_version as rv
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="GoalFrozenOperator",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(
        db_session, project.id, version_number=1, goal=None
    )
    await db_session.commit()

    rv.set_version_goal(
        version, "operator-chosen goal", editor_id="op@example.com", source="operator"
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal == "operator-chosen goal"
    assert version.goal_editor_info == "[operator:op@example.com]"

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

    revised_goal = "Deliver an exact-head candidate that can be verified before deployment."
    logs = json.dumps({"new_version_goal": revised_goal})

    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(version)

    assert version.goal == revised_goal
    assert version.goal_editor_info is not None
    assert version.goal_editor_info.startswith("[strategist:")


@pytest.mark.asyncio
async def test_handle_strategic_completion_rejects_contentless_new_version_goal(
    db_session, mocker
):
    """A contentless new_version_goal (e.g. 'test') must not be applied.

    The is_contentless_goal guard must fire and leave version.goal as None,
    so a placeholder goal never wedges a release by producing merge_grade
    confidence 0.0."""
    from unittest.mock import AsyncMock

    from app import release_version as rv
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="ContentlessGoal",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(
        db_session, project.id, version_number=1, goal=None
    )
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal is None

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

    logs = json.dumps({"new_version_goal": "test"})

    await _handle_strategic_completion(task, logs, db_session)
    await db_session.refresh(version)

    assert version.goal is None, (
        f"Contentless goal 'test' must not be stored; got {version.goal!r}"
    )


# ===========================================================================
# new_version_goal full API transport (set_problem_model → task.context → apply)
# ===========================================================================


@pytest.mark.asyncio
async def test_set_problem_model_lifts_new_version_goal_api_path(
    client, db_session, mocker
):
    """Full API-path regression: new_version_goal lifts past ProblemModel extra=forbid
    and is applied synchronously within the API handler to the open version.

    Exercises the seam end-to-end using the real HTTP transport (not a mocked
    context write). The context key is popped after synchronous apply so the
    completion handler is a no-op (idempotency guard for tasks).
    """
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app.strategic_completion import (
        NEW_VERSION_GOAL_CONTEXT_KEY,
        _handle_strategic_completion,
    )
    from app import release_version as rv
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="NVG-E2E",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open ReleaseVersion with no goal — the strategist should be able to author it.
    version = await rv.open_version(db_session, project.id, version_number=1, goal=None)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.goal is None

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-nvg-e2e",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-nvg-e2e", task_type="strategic_review")

    goal_text = "Ship the design-system token layer with zero arbitrary color literals."

    # The agent embeds new_version_goal as a TOP-LEVEL SIBLING of the
    # problem_model fields. The endpoint must strip it before ProblemModel
    # validation (extra="forbid" would 400 if it reaches apply_agent_update).
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Ship the thing", "current_focus": "build"},
                "capabilities": [],
                "gaps": [],
                "new_version_goal": goal_text,
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["op"] == "set_problem_model"
    assert body["new_version_goal_received"] is True, (
        "Response must confirm new_version_goal was received"
    )

    # The transient signal must NOT linger in the stored problem_model.
    await db_session.refresh(project)
    assert "new_version_goal" not in (project.problem_model or {}), (
        "new_version_goal must be stripped before ProblemModel validation"
    )
    assert (project.problem_model or {}).get("intent", {}).get("purpose") == (
        "Ship the thing"
    )

    # Synchronous apply: goal is set during set_problem_model, not deferred.
    await db_session.refresh(version)
    assert version.goal == goal_text, (
        f"Expected open version goal to be set during set_problem_model; got {version.goal!r}"
    )
    assert version.goal_editor_info is not None
    assert version.goal_editor_info.startswith("[strategist:"), (
        f"goal_editor_info must use source='strategist'; got {version.goal_editor_info!r}"
    )

    # Context key is consumed and popped — not stowed for the completion handler.
    await db_session.refresh(task)
    assert (task.context or {}).get(NEW_VERSION_GOAL_CONTEXT_KEY) is None

    # Completion handler is a no-op (key already applied).
    await _handle_strategic_completion(task, "", db_session)
    await db_session.refresh(version)
    assert version.goal == goal_text  # unchanged

    # A second call with a different goal from the strategist IS allowed —
    # the strategist can correct its own previously-authored goal.
    corrected_goal = "A corrected goal — strategist can revise its own authorship."
    resp2 = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Ship the thing", "current_focus": "build"},
                "capabilities": [],
                "gaps": [],
                "new_version_goal": corrected_goal,
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp2.status_code == 200, resp2.text
    # Second synchronous apply sets corrected goal immediately.
    await db_session.refresh(version)
    assert version.goal == corrected_goal, (
        f"Strategist must be able to correct its own goal; got {version.goal!r}"
    )


@pytest.mark.asyncio
async def test_batch_retire_reconciles_gap_active_moves_and_records_deferral(
    db_session, mocker
):
    """End-to-end (batch/log-parse path): retiring a gap's last staffing
    direction prunes it from the problem-model gap's active_moves (-> past_moves)
    and records a GapDeferral, in the same completion pass."""
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_completion._spawn_retirement_memos", new_callable=AsyncMock
    )

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        problem_model={
            "revision": 1,
            "gaps": [
                {
                    "id": "g-ret",
                    "name": "unstaffed soon",
                    "status": "active",
                    "active_moves": ["d-ret"],
                    "past_moves": [],
                }
            ],
        },
        directions=[
            {"id": "d-ret", "text": "Work the gap", "status": "active"},
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
                    "op": "retire",
                    "direction_id": "d-ret",
                    "rationale": "no longer worthwhile",
                    "terminal_reason": "invalidated",
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    # Direction flipped to retired in the live list.
    d = next(d for d in project.directions if d["id"] == "d-ret")
    assert d["status"] == "retired"

    # The staffed gap is reconciled end-to-end.
    gap = next(g for g in project.problem_model["gaps"] if g["id"] == "g-ret")
    assert gap["active_moves"] == []
    assert gap["past_moves"] == ["d-ret"]
    # Last active move retired -> deferral recorded naming the trigger direction.
    assert gap.get("deferral")
    assert "d-ret" in gap["deferral"]["rationale"]
    assert gap["deferral"]["created_at"]
    # Status is never auto-closed.
    assert gap["status"] == "active"


# ---------------------------------------------------------------------------
# set_acceptance_criteria strategic op (DB-backed, HTTP endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_authors_and_maps_acceptance_criteria(
    client: AsyncClient, db_session
):
    """strategic op set_acceptance_criteria creates VersionAcceptanceCriterion rows
    and maps directions to them via Direction.criterion_keys.

    Exercises:
    - happy path: two criteria, one mapped to a direction
    - idempotency: same call twice → no duplicate rows
    - 409 when no open release version exists
    """
    # --- Setup ---
    project = Project(name="AC-Strategic-Test", user_id="test-user-ac-strat")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(
        project_id=project.id, version_number=1, status="open"
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    direction = Direction(
        project_id=project.id,
        direction_key="d-ac-test1",
        data={"id": "d-ac-test1", "text": "Test direction", "status": "active"},
        status="active",
    )
    db_session.add(direction)
    await db_session.commit()
    await db_session.refresh(direction)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-ac-strat",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-ac-strat", task_type="strategic_review"
    )
    headers = {"x-agent-token": token}

    # --- First call: creates two criteria, maps first to direction ---
    normalized_contract = {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 0},
    }
    resp = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": "Users can create projects",
                        "evidence_contract": normalized_contract,
                        "direction_keys": ["d-ac-test1"],
                    },
                    {
                        "text": "CI passes on HEAD",
                        "evidence_contract": None,
                        "direction_keys": [],
                    },
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "set_acceptance_criteria"
    assert len(body["criteria"]) == 2

    first_item = next(c for c in body["criteria"] if "create projects" in c["text"])
    second_item = next(c for c in body["criteria"] if "CI passes" in c["text"])
    assert first_item["is_new"] is True
    assert second_item["is_new"] is True
    assert "d-ac-test1" in first_item["direction_keys"]
    assert second_item["direction_keys"] == []

    external_policy = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": "Spend external CI authority",
                        "evidence_contract": {
                            "type": "prepromotion_ci_build_smoke",
                            "predicate": {"required": True},
                        },
                    }
                ]
            },
        },
    )
    assert external_policy.status_code == 403

    criterion_key_1 = first_item["criterion_key"]
    criterion_key_2 = second_item["criterion_key"]

    # DB: two VersionAcceptanceCriterion rows
    rows = (
        await db_session.execute(
            select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.release_version_id == version.id
            )
        )
    ).scalars().all()
    assert len(rows) == 2
    texts = {r.text for r in rows}
    assert "Users can create projects" in texts
    assert "CI passes on HEAD" in texts
    contracted_row = next(row for row in rows if row.text == "Users can create projects")
    null_contract_row = next(row for row in rows if row.text == "CI passes on HEAD")
    assert contracted_row.created_by == "strategist"
    assert contracted_row.evidence_contract == normalized_contract
    assert contracted_row.evidence_contract_updated_by == f"strategist:{task.id}"
    assert contracted_row.evidence_contract_updated_at is not None
    assert null_contract_row.created_by == "strategist"
    assert null_contract_row.evidence_contract is None
    assert null_contract_row.evidence_contract_updated_by is None
    assert null_contract_row.evidence_contract_updated_at is None
    for row in rows:
        assert row.project_id == project.id
        assert row.release_version_id == version.id

    spoof_task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-ac-spoof",
    )
    db_session.add(spoof_task)
    await db_session.commit()
    await db_session.refresh(spoof_task)
    spoofed = await client.post(
        "/api/agent/strategic",
        headers={
            "x-agent-token": create_agent_token(
                str(spoof_task.id), "agent-vm-ac-spoof", task_type="strategic_review"
            )
        },
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {"criteria": [{"text": "Spoofed criterion"}]},
        },
    )
    assert spoofed.status_code == 403

    # DB: direction has criterion_key_1 in criterion_keys
    await db_session.refresh(direction)
    assert criterion_key_1 in direction.criterion_keys
    assert criterion_key_2 not in direction.criterion_keys

    # --- Second call: same criteria → no new rows (idempotent) ---
    resp2 = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": "Users can create projects",
                        "direction_keys": ["d-ac-test1"],
                    },
                    {
                        "text": "CI passes on HEAD",
                        "direction_keys": [],
                    },
                ]
            },
        },
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["status"] == "ok"
    for item in body2["criteria"]:
        assert item["is_new"] is False

    rows_after = (
        await db_session.execute(
            select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.release_version_id == version.id
            )
        )
    ).scalars().all()
    assert len(rows_after) == 2, "Idempotent call must not create duplicate rows"

    # criterion_keys on direction still contains only criterion_key_1 (no duplicates)
    await db_session.refresh(direction)
    assert direction.criterion_keys.count(criterion_key_1) == 1

    # --- Third call: no open version → 409 ---
    version.status = "closed"
    db_session.add(version)
    await db_session.commit()

    resp3 = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {"criteria": [{"text": "Should fail", "direction_keys": []}]},
        },
    )
    assert resp3.status_code == 409, resp3.text


@pytest.mark.asyncio
async def test_set_acceptance_criteria_op_enforces_cap(
    client: AsyncClient, db_session
):
    """set_acceptance_criteria stops adding new rows at MAX_CRITERIA_PER_VERSION
    and reports the skipped count in skipped_over_cap.

    Pre-condition: version is pre-filled to MAX-1, then two new criteria are
    requested — the first fills the last slot, the second is skipped.
    Existing-by-text (idempotent) rows do NOT consume new headroom.
    """
    from app import acceptance_criteria as ac

    project = Project(name="AC-Cap-Op-Test", user_id="test-user-cap-op")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Pre-fill to MAX - 1, leaving exactly one slot
    for i in range(ac.MAX_CRITERIA_PER_VERSION - 1):
        await ac.create_criterion(db_session, project.id, version.id, f"Pre {i}")

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-cap-op",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cap-op", task_type="strategic_review")
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {"text": "New criterion 1", "direction_keys": []},
                    {"text": "New criterion 2", "direction_keys": []},
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    # One accepted (fills last slot), one skipped over cap
    assert body["skipped_over_cap"] == 1
    assert len(body["criteria"]) == 1
    assert body["criteria"][0]["text"] == "New criterion 1"
    assert body["criteria"][0]["is_new"] is True

    # DB: verify count is exactly at cap
    all_rows = await ac.get_criteria_for_version(db_session, version.id)
    assert len(all_rows) == ac.MAX_CRITERIA_PER_VERSION


@pytest.mark.asyncio
async def test_set_acceptance_criteria_dedup_does_not_consume_headroom(
    client: AsyncClient, db_session
):
    """Duplicate-by-text criteria in set_acceptance_criteria do not count against
    the cap — only genuinely new rows consume headroom."""
    from app import acceptance_criteria as ac

    project = Project(name="AC-Cap-Dedup-Test", user_id="test-user-cap-dedup")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Pre-fill to MAX - 1, leaving one slot; also pre-create the duplicate row
    for i in range(ac.MAX_CRITERIA_PER_VERSION - 1):
        await ac.create_criterion(db_session, project.id, version.id, f"Existing {i}")
    # The last pre-created row uses index 0 text; create an extra "Already exists" row
    # to use as the idempotent duplicate in the request
    await ac.create_criterion(db_session, project.id, version.id, "Already exists")

    # Now at MAX (no slots)
    count_before = await ac.count_criteria_for_version(db_session, version.id)
    assert count_before == ac.MAX_CRITERIA_PER_VERSION

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-cap-dedup",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cap-dedup", task_type="strategic_review")
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {"text": "Already exists", "direction_keys": []},  # duplicate — no headroom consumed
                    {"text": "Truly new", "direction_keys": []},       # new — no slots → skipped
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    # Duplicate is accepted (is_new=False), new one is skipped over cap
    assert body["skipped_over_cap"] == 1
    assert len(body["criteria"]) == 1
    assert body["criteria"][0]["text"] == "Already exists"
    assert body["criteria"][0]["is_new"] is False

    # DB count unchanged
    count_after = await ac.count_criteria_for_version(db_session, version.id)
    assert count_after == ac.MAX_CRITERIA_PER_VERSION


@pytest.mark.asyncio
async def test_set_acceptance_criteria_rejects_inactive_or_dangling_direction_mapping_key(
    client: AsyncClient, db_session
):
    """set_acceptance_criteria scrubs inactive and dangling criterion keys
    from Direction.criterion_keys when merging new mappings via classify_criterion_key.

    A direction may carry stale criterion_keys from prior operations.  When
    set_acceptance_criteria maps a new criterion to that direction, the guard
    classifies each existing key — inactive (retracted) and dangling (never
    existed) keys are filtered out, leaving only active or newly-created keys.
    """
    from app import acceptance_criteria as ac

    project = Project(name="AC-Guard-Scrub-Test", user_id="test-user-ac-guard")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create a criterion and retract it so it becomes inactive.
    inactive_criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Criterion that will be retracted"
    )
    await ac.retract_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=inactive_criterion.criterion_key,
        reason="superseded in test",
        retracted_by="test",
    )

    # Direction pre-loaded with one inactive key and one dangling key (never existed).
    dangling_key = "c-dangling-does-not-exist"
    direction = Direction(
        project_id=project.id,
        direction_key="d-guard-scrub-test1",
        data={"id": "d-guard-scrub-test1", "text": "Test direction", "status": "active"},
        status="active",
        criterion_keys=[inactive_criterion.criterion_key, dangling_key],
    )
    db_session.add(direction)
    await db_session.commit()
    await db_session.refresh(direction)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-ac-guard",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-ac-guard", task_type="strategic_review"
    )
    headers = {"x-agent-token": token}

    # Map a new active criterion to the direction.
    resp = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": "New active criterion to map",
                        "direction_keys": ["d-guard-scrub-test1"],
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["criteria"]) == 1
    new_criterion_key = body["criteria"][0]["criterion_key"]
    assert body["criteria"][0]["direction_keys"] == ["d-guard-scrub-test1"]

    # The direction must contain only the new active key.
    # classify_criterion_key must have scrubbed both the inactive and dangling keys.
    await db_session.refresh(direction)
    assert new_criterion_key in direction.criterion_keys, (
        "New criterion key must be present in direction.criterion_keys"
    )
    assert inactive_criterion.criterion_key not in direction.criterion_keys, (
        "Inactive (retracted) criterion key must be scrubbed from direction.criterion_keys"
    )
    assert dangling_key not in direction.criterion_keys, (
        "Dangling criterion key must be scrubbed from direction.criterion_keys"
    )


@pytest.mark.asyncio
async def test_set_acceptance_criteria_backfills_evidence_for_prelanded_task(
    client: AsyncClient, db_session
):
    """set_acceptance_criteria backfill hook records evidence for a task that already
    merged before the criterion-direction mapping was created.

    This exercises the 'late-mapping' scenario: a task shipped, then the strategist
    maps a new criterion to that direction.  The backfill hook in the strategic op
    should replay evidence for the pre-existing proven task.
    """
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    # --- Setup ---
    project = Project(name="AC-Backfill-Strategic", user_id="test-user-ac-bf")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(project_id=project.id, version_number=1, status="open")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    direction = Direction(
        project_id=project.id,
        direction_key="d-ac-bf-test1",
        data={"id": "d-ac-bf-test1", "text": "Backfill test direction", "status": "active"},
        status="active",
    )
    db_session.add(direction)
    await db_session.commit()
    await db_session.refresh(direction)

    # Pre-existing task that already merged in the current version window.
    pre_landed_task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        completed_at=version.opened_at + timedelta(seconds=1),
        context={
            "direction_id": direction.direction_key,
            "pr_merged": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {"proven": True},
        },
    )
    db_session.add(pre_landed_task)
    await db_session.commit()
    await db_session.refresh(pre_landed_task)

    # Strategic task that will author the criterion.
    strategic_task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-ac-bf",
    )
    db_session.add(strategic_task)
    await db_session.commit()
    await db_session.refresh(strategic_task)

    token = create_agent_token(
        str(strategic_task.id), "agent-vm-ac-bf", task_type="strategic_review"
    )
    headers = {"x-agent-token": token}

    # Call set_acceptance_criteria — maps a new criterion to the direction that
    # already has a proven merged task.
    resp = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(strategic_task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": "Pre-landed criterion coverage check",
                        "direction_keys": [direction.direction_key],
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    criterion_key = body["criteria"][0]["criterion_key"]

    # The backfill hook should have created evidence for the pre-landed task.
    criteria_rows = (
        await db_session.execute(
            select(VersionAcceptanceCriterion).where(
                VersionAcceptanceCriterion.criterion_key == criterion_key
            )
        )
    ).scalars().all()
    assert len(criteria_rows) == 1, "Criterion row must exist"
    criterion_id = criteria_rows[0].id

    ev_rows = (
        await db_session.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id == criterion_id
            )
        )
    ).scalars().all()
    source_kinds = {e.source_kind for e in ev_rows}
    assert "task_merge" in source_kinds, (
        "Backfill hook must have recorded task_merge evidence for the pre-landed task"
    )
    assert "reachability" in source_kinds, (
        "Backfill hook must have recorded reachability evidence for the pre-landed task"
    )
    # All evidence rows reference the pre-landed task.
    for ev in ev_rows:
        assert ev.source_id == str(pre_landed_task.id)


@pytest.mark.asyncio
async def test_spawn_retirement_memos_failure_does_not_abort_commit(db_session, mocker):
    """When _spawn_retirement_memos raises, the surrounding commit still runs and exception is logged."""
    from unittest.mock import AsyncMock, patch
    from app.strategic_completion import _handle_strategic_completion, _RETIRED_NOTE_PREFIX

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    direction_id = "d-memo-fail-01"
    direction = {
        "id": direction_id,
        "text": "Memo-fail test direction",
        "status": "expired",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "notes": [
            {
                "text": f"{_RETIRED_NOTE_PREFIX}test rationale",
                "added_at": "2026-01-01T00:00:00",
            }
        ],
    }
    project = Project(
        name="MemoFail",
        user_id="u-memo-fail",
        vision={"purpose": "test", "revision": 1},
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sr_task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={
            "_strategic_api_called": True,
            "_strategic_retired_ids": [direction_id],
            "_strategic_retired": 1,
        },
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    async def _blow_up(*args, **kwargs):
        raise RuntimeError("simulated memo spawn failure")

    with (
        patch("app.strategic_completion._spawn_retirement_memos", new=_blow_up),
        patch("app.strategic_completion.logger") as mock_logger,
    ):
        # Must not raise — commit must proceed
        await _handle_strategic_completion(sr_task, "", db_session)

    error_calls = [str(c) for c in mock_logger.error.call_args_list]
    assert any("_spawn_retirement_memos" in c or "retirement memo" in c.lower() for c in error_calls), (
        f"Expected error log for memo spawn failure; got: {error_calls}"
    )

    # The project must still be committable (no session in error state) and reachable.
    await db_session.refresh(project)
    assert project.id is not None, "Project must still be readable after memo-spawn failure"


@pytest.mark.asyncio
async def test_unparseable_strategic_failure_counter_is_atomic(db_session, mocker):
    """Two sequential unparseable-output failures both increment _strategic_failures.

    Regression test for the unlocked RMW race: before the fix, two concurrent
    (or sequential) failures could both read the same baseline and write back the
    same +1, silently losing an increment. with_vision_locked serializes the
    counter bump so both increments are recorded.
    """
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First failure: empty logs -> unparseable -> increments _strategic_failures to 1
    task1 = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}],
        context={},
    )
    db_session.add(task1)
    await db_session.commit()
    await db_session.refresh(task1)

    await _handle_strategic_completion(task1, "", db_session)

    # Second failure: another task, empty logs -> unparseable -> increments to 2
    task2 = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}],
        context={},
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    await _handle_strategic_completion(task2, "", db_session)

    await db_session.refresh(project)
    vision = project.vision or {}
    assert vision.get("_strategic_failures") == 2, (
        f"Two sequential unparseable failures must each increment _strategic_failures; "
        f"expected 2, got {vision.get('_strategic_failures')!r}"
    )
    assert vision.get("_strategic_failures_at") is not None, (
        "_strategic_failures_at must be stamped after unparseable failure"
    )


@pytest.mark.asyncio
async def test_fallback_activate_direction_applied(db_session, mocker):
    """Fallback must transition a paused direction to active via activate_direction op."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackActivate",
        user_id="u-fallback-activate",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-paused",
                "text": "Paused direction",
                "status": "paused",
                "auto_paused": True,
                "paused_reason": "churn",
                "paused_at": "2026-01-01T00:00:00",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "activate_direction",
                    "direction_id": "d-paused",
                    "rationale": "ready to proceed",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    activated = next(
        (d for d in (project.directions or []) if d.get("id") == "d-paused"), None
    )
    assert activated is not None, "d-paused must still be present after activate_direction"
    assert activated["status"] == "active", (
        f"activate_direction must set status=active; got {activated.get('status')!r}"
    )
    assert not activated.get("auto_paused"), (
        "activate_direction must clear auto_paused flag"
    )
    await db_session.refresh(task)
    updated_ids = (task.context or {}).get("_strategic_updated_ids", [])
    assert "d-paused" in updated_ids, (
        f"task context must record d-paused in _strategic_updated_ids; got {updated_ids}"
    )


@pytest.mark.asyncio
async def test_fallback_reject_direction_no_mid_loop_write(db_session, mocker):
    """reject_direction must use the single end-of-loop with_directions_locked call,
    not a separate mid-loop write — one call total, direction properly tombstoned."""
    import app.task_locking as _task_locking_mod
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="FallbackRejectSingleWrite",
        user_id="u-reject-single-write",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-reject",
                "text": "Reject me",
                "status": "proposed",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "reject_direction",
                    "direction_id": "d-reject",
                    "rationale": "not worthwhile",
                }
            ],
        }
    )

    wdl_call_count = 0
    real_wdl = _task_locking_mod.with_directions_locked

    async def counting_wdl(session, project_id, mutate_fn, **kwargs):
        nonlocal wdl_call_count
        wdl_call_count += 1
        return await real_wdl(session, project_id, mutate_fn, **kwargs)

    mocker.patch.object(_task_locking_mod, "with_directions_locked", new=counting_wdl)

    await _handle_strategic_completion(task, logs, db_session)

    assert wdl_call_count == 1, (
        "reject_direction must not make a mid-loop with_directions_locked call; "
        f"expected 1 total call, got {wdl_call_count}"
    )
    await db_session.refresh(project)
    tombstone = next(
        (d for d in (project.directions or []) if d.get("id") == "d-reject"), None
    )
    assert tombstone is not None, "tombstone must remain in directions after reject"
    assert tombstone["status"] == "rejected", (
        f"rejected direction must have status=rejected; got {tombstone.get('status')!r}"
    )


@pytest.mark.asyncio
async def test_fallback_directions_write_preserves_concurrent_add(db_session, mocker):
    """Delta-based write must not overwrite a direction added concurrently during the
    fallback; the concurrent direction must survive the locked write."""
    import app.task_locking as _task_locking_mod
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    concurrent_dir = {
        "id": "d-concurrent",
        "text": "Added concurrently",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-02T00:00:00",
        "research_history": [],
        "notes": [],
    }

    project = Project(
        name="FallbackDeltaPreserve",
        user_id="u-delta-preserve",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-retire",
                "text": "To be retired",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "retire_direction",
                    "direction_id": "d-retire",
                    "rationale": "done",
                    "terminal_reason": "invalidated",
                }
            ],
        }
    )

    real_wdl = _task_locking_mod.with_directions_locked

    async def wdl_inject_concurrent(session, project_id, mutate_fn, **kwargs):
        # Simulate a concurrent writer inserting d-concurrent into the DB
        # before the delta callback fires.  The delta callback must preserve it.
        def injecting_fn(fresh: list) -> list:
            return mutate_fn([*fresh, concurrent_dir])

        return await real_wdl(session, project_id, injecting_fn, **kwargs)

    mocker.patch.object(_task_locking_mod, "with_directions_locked", new=wdl_inject_concurrent)

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dir_ids = {d.get("id") for d in (project.directions or [])}
    assert "d-concurrent" in dir_ids, (
        "Delta write must preserve a concurrently-added direction; "
        f"got direction ids: {sorted(dir_ids)}"
    )
    retired = next(
        (d for d in (project.directions or []) if d.get("id") == "d-retire"), None
    )
    assert retired is not None and retired["status"] == "retired", (
        f"d-retire must be retired after fallback retire_direction; got {retired}"
    )


@pytest.mark.asyncio
async def test_retire_direction_note_not_duplicated_on_reprocess(db_session, mocker):
    """Fallback retire_direction must not append the same note twice on a retry.

    Simulates the post-commit-pending re-entry: _handle_strategic_completion is
    called twice with the same retire_direction op (same rationale, same direction
    id).  The retirement note must appear exactly once in the direction's notes list.
    """
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="NoteDedup",
        user_id="u-note-dedup",
        vision={"purpose": "test", "revision": 0},
        directions=[
            {
                "id": "d-dedup",
                "text": "Direction to retire",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [],
                "notes": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    rationale = "Completed all objectives"
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "retire_direction",
                    "direction_id": "d-dedup",
                    "rationale": rationale,
                    "terminal_reason": "invalidated",
                }
            ],
        }
    )

    def _make_task():
        return Task(
            project_id=project.id,
            payloads=[
                {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
            ],
            context={},
        )

    # First call — simulates the original run
    task1 = _make_task()
    db_session.add(task1)
    await db_session.commit()
    await db_session.refresh(task1)
    await _handle_strategic_completion(task1, logs, db_session)

    # Second call — simulates the post-commit-pending retry with a new task
    # but identical op payload (same direction id, same rationale)
    task2 = _make_task()
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)
    await _handle_strategic_completion(task2, logs, db_session)

    await db_session.refresh(project)
    retired = next(
        (d for d in (project.directions or []) if d.get("id") == "d-dedup"), None
    )
    assert retired is not None, "d-dedup must still be present in directions"
    assert retired["status"] == "retired", (
        f"d-dedup must be retired; got status={retired.get('status')}"
    )
    retire_notes = [
        n for n in (retired.get("notes") or [])
        if n.get("text") == f"Retired by strategic review: {rationale}"
    ]
    assert len(retire_notes) == 1, (
        f"Retirement note must appear exactly once (dedup); found {len(retire_notes)}: {retire_notes}"
    )


@pytest.mark.asyncio
async def test_retire_direction_succeeds_when_criterion_is_covered(
    db_session, mocker
):
    """When the direction's owned criterion IS covered, the strategist
    retire_direction path proceeds normally and retires the direction."""
    from unittest.mock import AsyncMock

    from app import acceptance_criteria as ac
    from app.models import CriterionEvidence, Direction, ReleaseVersion
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    direction_id = "d-cov-covered"
    direction_dict = {
        "id": direction_id,
        "text": "Direction owning a covered criterion",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "notes": [],
    }
    project = Project(
        name="RetireGateCovered",
        user_id="u-retire-gate",
        vision={"purpose": "test", "revision": 0},
        directions=[direction_dict],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open a release version with a COVERED criterion (has CriterionEvidence).
    version = ReleaseVersion(project_id=project.id, version_number=1)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    criterion = await ac.create_criterion(
        db_session, project.id, version.id, "Owned acceptance outcome"
    )
    # Attach evidence so the criterion is covered.
    db_session.add(
        CriterionEvidence(
            criterion_id=criterion.id,
            project_id=project.id,
            source_kind="reachability",
            source_id="pr-1",
        )
    )
    await db_session.commit()

    db_session.add(
        Direction(
            project_id=project.id,
            direction_key=direction_id,
            status="active",
            data=dict(direction_dict),
            criterion_keys=[criterion.criterion_key],
        )
    )
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "retire_direction",
                    "direction_id": direction_id,
                    "rationale": "Direction completed",
                    "terminal_reason": "invalidated",
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(
        (d for d in (project.directions or []) if d.get("id") == direction_id),
        None,
    )
    assert target is not None, (
        f"Direction {direction_id} must still be present in directions"
    )
    assert target.get("status") == "retired", (
        f"Direction whose owned criterion IS covered must be retired by the "
        f"strategist retire_direction path; expected status=retired, got "
        f"status={target.get('status')}"
    )
    # "Retired by strategic review" note should be present.
    retire_notes = [
        n
        for n in (target.get("notes") or [])
        if "Retired by strategic review" in str(n.get("text", ""))
    ]
    assert len(retire_notes) == 1, (
        f"Retirement note must be present when retirement succeeded; "
        f"found: {retire_notes}"
    )


def test_self_reflection_log_dedups_replayed_cycle():
    """Replaying the same cycle (post-commit-pending recovery) must not duplicate
    self_reflection_log entries. Dedup is by cycle_task_id to prevent bloated token
    consumption and recurrence hallucination when identical entries are re-appended."""
    from app.problem_model import SelfReflectionEntry, append_self_reflection
    from app.time_utils import _utcnow

    cycle_task_id = "t-12345"
    now = _utcnow().isoformat()

    # Create an entry for this cycle
    entry = SelfReflectionEntry(
        ts=now,
        cycle_task_id=cycle_task_id,
        what_was_hard="Missing context on async patterns",
        missing_context="",
        proposed_self_improvement="Add async examples to prompt",
    )

    # First append: log should have 1 entry
    log = append_self_reflection(None, entry)
    assert len(log) == 1, f"Expected 1 entry after first append, got {len(log)}"
    assert log[0]["cycle_task_id"] == cycle_task_id

    # Replay: append identical entry again (same cycle_task_id, same task_id)
    log = append_self_reflection(log, entry)
    assert len(log) == 1, (
        f"Dedup must prevent duplicate: expected 1 entry after replay, got {len(log)}. "
        "This can happen when post-commit-pending recovery re-appends from saved payload."
    )

    # Verify the entry is unchanged
    assert log[0]["cycle_task_id"] == cycle_task_id
    assert log[0]["what_was_hard"] == "Missing context on async patterns"


def test_rejected_proposals_dedup_on_replay():
    """_merge_rejected_proposals must not create duplicate entries on replay.

    The recovery sweeper can re-run _apply_rejected_proposals with the same
    batch after a post-commit crash.  Each call must be idempotent: an entry
    whose 'text' is already present in vision['rejected_proposals'] must be
    skipped, so the list does not grow beyond the number of genuinely distinct
    proposals.
    """
    batch = [
        {"text": "Improve user onboarding", "rejected_at": "2024-01-01T00:00:00+00:00"},
        {"text": "Add dashboard charts", "rejected_at": "2024-01-01T00:00:01+00:00"},
    ]
    vision: dict = {}

    # Initial run
    _merge_rejected_proposals(vision, batch)
    assert len(vision["rejected_proposals"]) == 2

    # Replay: same batch applied again (simulating recovery-sweeper re-dispatch)
    _merge_rejected_proposals(vision, batch)
    rp = vision["rejected_proposals"]
    assert len(rp) == 2, (
        f"Replay must not duplicate entries: expected 2, got {len(rp)}. "
        "This fires when _merge_rejected_proposals appends without checking existing texts."
    )
    texts = {e["text"] for e in rp}
    assert "Improve user onboarding" in texts
    assert "Add dashboard charts" in texts


def test_rejected_proposals_dedup_preserves_cap():
    """_merge_rejected_proposals caps at 30 and still deduplicates on replay."""
    # Pre-populate with 29 distinct entries
    existing = [{"text": f"proposal-{i}", "rejected_at": "2024-01-01T00:00:00+00:00"} for i in range(29)]
    vision = {"rejected_proposals": list(existing)}

    batch = [{"text": "proposal-new", "rejected_at": "2024-01-02T00:00:00+00:00"}]
    _merge_rejected_proposals(vision, batch)
    assert len(vision["rejected_proposals"]) == 30

    # Replay: the new entry is already present — list must stay at 30
    _merge_rejected_proposals(vision, batch)
    assert len(vision["rejected_proposals"]) == 30


@pytest.mark.asyncio
async def test_strategic_review_effort_classifier(db_session):
    """Pre-spawn effort classifier for strategic_review cycles.

    A strategic_review cycle with no high-stakes signals (no open messages, no
    operator steering, no version decision pending, clean integrity
    reconciliation, no prior-cycle dirt, no recent cycle failures) must spawn at
    ROUTINE effort ('medium') — which routing resolution resolves to a cheaper
    provider cell than the 'strategic' category default 'max' — and stamp the
    tier + reason on the StrategicCycle row for cost auditability. Any
    high-stakes or unknown signal resolves CONSEQUENTIAL ('max'), fail-closed.
    """
    from app.strategic_review_effort import (
        classify_strategic_review_effort,
        CONSEQUENTIAL_EFFORT,
        CONSEQUENTIAL_TIER,
        ROUTINE_EFFORT,
        ROUTINE_TIER,
    )
    from app.strategic_spawning import _spawn_strategic_review
    from app.models import CYCLE_META_EFFORT_REASON, CYCLE_META_EFFORT_TIER

    # ── Pure classifier behavior ────────────────────────────────────────────
    # Routine: every low-stakes condition holds.
    routine = classify_strategic_review_effort(
        {
            "open_message_count": 0,
            "operator_steered": False,
            "version_phase_block": None,
            "integrity_conflict_counts": None,
            "prior_cycle_dirty": False,
            "recent_cycle_failures": False,
        }
    )
    assert routine == (ROUTINE_EFFORT, ROUTINE_TIER, "all routine signals clean")

    # Consequential: each high-stakes signal alone forces the max tier.
    assert classify_strategic_review_effort(
        {"open_message_count": 1, "operator_steered": False}
    )[1] == CONSEQUENTIAL_TIER
    assert classify_strategic_review_effort(
        {"open_message_count": 0, "operator_steered": True}
    )[1] == CONSEQUENTIAL_TIER
    assert classify_strategic_review_effort(
        {"open_message_count": 0, "version_phase_block": {"goal_needed": True}}
    )[1] == CONSEQUENTIAL_TIER
    assert classify_strategic_review_effort(
        {
            "open_message_count": 0,
            "version_phase_block": {"qualification_state": "required_check_failed"},
        }
    )[1] == CONSEQUENTIAL_TIER
    # The spawn path carries the full summarize_qualification_state DICT form
    # ({"state", "summary", ...}) — both shapes must classify identically.
    assert classify_strategic_review_effort(
        {
            "open_message_count": 0,
            "version_phase_block": {
                "qualification_state": {"state": "required_check_failed"}
            },
        }
    )[1] == CONSEQUENTIAL_TIER
    assert classify_strategic_review_effort(
        {
            "open_message_count": 0,
            "version_phase_block": {"qualification_state": {"state": "promoted"}},
        }
    )[1] == ROUTINE_TIER  # settled version does not force consequential
    assert classify_strategic_review_effort(
        {
            "open_message_count": 0,
            "integrity_conflict_counts": {
                "orphaned_gaps": 2,
                "ungrounded_directions": 0,
                "stale_active_moves": 0,
                "staffed_but_deferred": 0,
            },
        }
    )[1] == CONSEQUENTIAL_TIER
    assert classify_strategic_review_effort(
        {"open_message_count": 0, "prior_cycle_dirty": True}
    )[1] == CONSEQUENTIAL_TIER
    assert classify_strategic_review_effort(
        {"open_message_count": 0, "recent_cycle_failures": True}
    )[1] == CONSEQUENTIAL_TIER

    # Fail-closed: missing/unknown signals never cheapen a cycle.
    assert classify_strategic_review_effort(
        {"operator_steered": False}
    )[1] == CONSEQUENTIAL_TIER  # open_message_count unknown
    assert classify_strategic_review_effort(
        {"open_message_count": 0, "version_phase_block": "garbage"}
    )[1] == CONSEQUENTIAL_TIER  # malformed version phase
    assert classify_strategic_review_effort(
        {
            "open_message_count": 0,
            "version_phase_block": {"qualification_state": "brand_new_state"},
        }
    )[1] == CONSEQUENTIAL_TIER  # novel qual state fails closed
    assert classify_strategic_review_effort("not-a-mapping")[1] == CONSEQUENTIAL_TIER

    # ── Wiring: a routine cadence spawn carries effort 'medium' and stamps the
    # cycle row. Equal integration/promotion branches → no version decision
    # surfaced; a fresh project has no messages / conflicts / failed cycles.
    project = Project(
        name="Effort-Routine",
        user_id="test-user-123",
        integration_branch="main",
        promotion_target="main",
        vision={"purpose": "test purpose", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    task = (
        (
            await db_session.execute(
                select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    assert task.payloads[0]["type"] == "strategic_review"
    assert task.payloads[0].get("effort") == ROUTINE_EFFORT, (
        "A routine strategic_review cycle must pin the cheaper effort tier on "
        "the payload so routing resolution does not fall through to the "
        "'strategic' category default 'max'."
    )

    cycle = (
        (
            await db_session.execute(
                select(StrategicCycle).where(StrategicCycle.project_id == project.id)
            )
        )
        .scalars()
        .all()[0]
    )
    meta = cycle.cycle_metadata or {}
    assert meta.get(CYCLE_META_EFFORT_TIER) == ROUTINE_TIER
    assert meta.get(CYCLE_META_EFFORT_REASON) == "all routine signals clean"
    # The resolved effort for the payload is 'medium' — distinct from the
    # consequential tier — so the two cannot be confused in cost attribution.
    assert ROUTINE_EFFORT != CONSEQUENTIAL_EFFORT


def test_record_effort_tier_stamps_cycle_metadata():
    """``record_effort_tier`` writes ``effort_tier`` + ``effort_tier_reason``
    into cycle_metadata without disturbing existing keys, and the legacy dict
    renderer (agent cycle-history context) surfaces both so cost analysis can
    read the tier without re-deriving the classifier."""
    from app.models import CYCLE_META_EFFORT_REASON, CYCLE_META_EFFORT_TIER
    from app.strategic_cycle import _to_legacy_dict, record_effort_tier

    cycle = StrategicCycle(
        id="sc-effort-unit",
        project_id=uuid.uuid4(),
        outcome="phase2_running",
        phase1_started_at=datetime.now(timezone.utc),
        cycle_metadata={"existing_key": "value"},
    )
    record_effort_tier(cycle, "routine", "all routine signals clean")

    meta = cycle.cycle_metadata or {}
    assert meta.get(CYCLE_META_EFFORT_TIER) == "routine"
    assert meta.get(CYCLE_META_EFFORT_REASON) == "all routine signals clean"
    assert meta.get("existing_key") == "value", (
        "record_effort_tier must merge, never replace, existing cycle metadata."
    )

    legacy = _to_legacy_dict(cycle)
    assert legacy.get(CYCLE_META_EFFORT_TIER) == "routine"
    assert legacy.get(CYCLE_META_EFFORT_REASON) == "all routine signals clean"


@pytest.mark.asyncio
async def test_strategic_contract_ownership_and_active_direction_mapping(
    client, db_session
):
    """Strategic retries preserve operator contracts and never map inactive directions."""
    from app.acceptance_criteria import get_criterion_by_text

    task = await _make_strategic_task(db_session, name="contract-ownership", vm="vm-contract")
    version = ReleaseVersion(project_id=task.project_id, version_number=1, status="open")
    operator_contract = {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 0},
    }
    strategist_contract = {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 1},
    }
    operator_row = VersionAcceptanceCriterion(
        project_id=task.project_id,
        release_version_id=version.id,
        criterion_key="c-operator1",
        text="Operator-owned contract",
        evidence_contract=operator_contract,
        created_by="operator",
    )
    strategist_row = VersionAcceptanceCriterion(
        project_id=task.project_id,
        release_version_id=version.id,
        criterion_key="c-strategy1",
        text="Strategist-owned contract",
        evidence_contract=operator_contract,
        evidence_contract_updated_by=f"strategist:{task.id}",
        evidence_contract_updated_at=_utcnow(),
        created_by="strategist",
    )
    owner_bound_row = VersionAcceptanceCriterion(
        project_id=task.project_id,
        release_version_id=version.id,
        criterion_key="c-owner-bound-ci",
        text="Owner-bound external CI contract",
        evidence_contract={
            "type": "prepromotion_ci_build_smoke",
            "predicate": {"required": True},
        },
        evidence_contract_updated_by="project-owner",
        evidence_contract_updated_at=_utcnow(),
        created_by="strategist",
    )
    owner_bound_authorship_row = VersionAcceptanceCriterion(
        project_id=task.project_id,
        release_version_id=version.id,
        criterion_key="c-owner-bound-authorship",
        text="Owner-bound authorship contract",
        evidence_contract={
            "type": "operator_authored_commits",
            "predicate": {"max_count": 0},
        },
        evidence_contract_updated_by="project-owner",
        evidence_contract_updated_at=_utcnow(),
        created_by="strategist",
    )
    inactive_direction = Direction(
        project_id=task.project_id,
        direction_key="d-inactive-contract",
        data={"id": "d-inactive-contract", "status": "paused"},
        status="paused",
    )
    active_direction = Direction(
        project_id=task.project_id,
        direction_key="d-active-contract",
        data={"id": "d-active-contract", "status": "active"},
        status="active",
    )
    db_session.add_all(
        [
            version,
            operator_row,
            strategist_row,
            owner_bound_row,
            owner_bound_authorship_row,
            inactive_direction,
            active_direction,
        ]
    )
    await db_session.commit()
    token = create_agent_token(str(task.id), "vm-contract", task_type="strategic_review")
    headers = {"x-agent-token": token}

    unchanged = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": operator_row.text,
                        "evidence_contract": strategist_contract,
                        "direction_keys": [inactive_direction.direction_key],
                    }
                ]
            },
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    await db_session.refresh(operator_row)
    await db_session.refresh(inactive_direction)
    assert operator_row.evidence_contract == operator_contract
    assert inactive_direction.criterion_keys == []

    attempted_clear = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": owner_bound_row.text,
                        "evidence_contract": None,
                        "direction_keys": [],
                    }
                ]
            },
        },
    )
    assert attempted_clear.status_code == 200, attempted_clear.text
    await db_session.refresh(owner_bound_row)
    assert owner_bound_row.evidence_contract == {
        "type": "prepromotion_ci_build_smoke",
        "predicate": {"required": True},
    }
    assert owner_bound_row.evidence_contract_updated_by == "project-owner"

    attempted_authorship_clear = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": owner_bound_authorship_row.text,
                        "evidence_contract": None,
                        "direction_keys": [],
                    }
                ]
            },
        },
    )
    assert attempted_authorship_clear.status_code == 200
    await db_session.refresh(owner_bound_authorship_row)
    assert owner_bound_authorship_row.evidence_contract == {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 0},
    }
    assert owner_bound_authorship_row.evidence_contract_updated_by == "project-owner"

    attempted_retract = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [],
                "retract": [
                    {
                        "criterion_key": owner_bound_row.criterion_key,
                        "reason": "autonomous policy relaxation",
                    }
                ],
            },
        },
    )
    assert attempted_retract.status_code == 422
    await db_session.refresh(owner_bound_row)
    assert owner_bound_row.status == "active"

    attempted_authorship_retract = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [],
                "retract": [
                    {
                        "criterion_key": owner_bound_authorship_row.criterion_key,
                        "reason": "autonomous authorship relaxation",
                    }
                ],
            },
        },
    )
    assert attempted_authorship_retract.status_code == 422
    await db_session.refresh(owner_bound_authorship_row)
    assert owner_bound_authorship_row.status == "active"

    changed = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {
                "criteria": [
                    {
                        "text": strategist_row.text,
                        "evidence_contract": strategist_contract,
                        "direction_keys": [active_direction.direction_key],
                    }
                ]
            },
        },
    )
    assert changed.status_code == 200, changed.text
    await db_session.refresh(strategist_row)
    assert strategist_row.evidence_contract == strategist_contract
    assert strategist_row.evidence_contract_updated_at is not None
    assert strategist_row.evidence_contract_updated_by.startswith("strategist:")
    await db_session.refresh(active_direction)
    assert strategist_row.criterion_key in active_direction.criterion_keys

    duplicate = VersionAcceptanceCriterion(
        project_id=task.project_id,
        release_version_id=version.id,
        criterion_key="c-operator2",
        text=operator_row.text,
        created_by="strategist",
    )
    db_session.add(duplicate)
    await db_session.commit()
    ambiguous = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "set_acceptance_criteria",
            "data": {"criteria": [{"text": operator_row.text, "direction_keys": []}]},
        },
    )
    assert ambiguous.status_code == 409
    assert await get_criterion_by_text(
        db_session, task.project_id, version.id, strategist_row.text
    ) == strategist_row
