"""Tests for pure direction lifecycle helpers in app.strategic_completion.

Covers:
  - _check_direction_health: saturation, zero-merge, high-failure-rate rules
  - _check_direction_completion: cycle/PR thresholds and recent-activity check
  - _is_direction_duplicate: word-overlap with existing and rejected proposals
  - _record_strategic_task_failure: circuit-breaker increment and cycle history update
"""
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
    _CREATED_NOTE_PREFIX,
    _RETIRED_NOTE_PREFIX,
)
from app.strategic_spawning import _build_previous_decision_outcomes, _build_past_retirement_critiques
from app.models import Project, Task


# ── Helpers ──────────────────────────────────────────────────────────────────

def _history(n, tasks_created=2, tasks_completed=1, tasks_failed=0,
             tasks_failed_infra=0, prs_merged=1):
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
            research_history=_history(5, prs_merged=0, tasks_created=2,
                                      tasks_completed=1, tasks_failed=1),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Unproductive" in result

    def test_zero_merge_requires_5_plus_cycles(self):
        d = _direction(
            research_history=_history(4, prs_merged=0, tasks_created=2,
                                      tasks_completed=2, tasks_failed=0),
        )
        result = _check_direction_health(d)
        assert result is None

    def test_zero_merge_skipped_when_tasks_still_in_flight(self):
        # tasks_created=3, tasks_completed=1, tasks_failed=1 → finished=2 < created=3
        history = _history(4, prs_merged=0) + [
            {"tasks_created": 3, "tasks_completed": 1, "tasks_failed": 1,
             "tasks_failed_infra": 0, "prs_merged": 0}
        ]
        d = _direction(research_history=history)
        result = _check_direction_health(d)
        assert result is None

    def test_zero_merge_allowed_when_last_cycle_has_zero_tasks(self):
        # tasks_created=0 → guard treats this as finished (0 == 0)
        history = _history(4, prs_merged=0) + [
            {"tasks_created": 0, "tasks_completed": 0, "tasks_failed": 0,
             "tasks_failed_infra": 0, "prs_merged": 0}
        ]
        d = _direction(research_history=history)
        result = _check_direction_health(d)
        assert result is not None
        assert "Unproductive" in result

    def test_zero_merge_not_fired_when_some_prs_merged(self):
        # 5 cycles but 1 merged PR in cycle 3
        history = _history(2, prs_merged=0) + _history(1, prs_merged=1) + _history(2, prs_merged=0)
        d = _direction(
            research_history=history + [
                {"tasks_created": 2, "tasks_completed": 2, "tasks_failed": 0,
                 "tasks_failed_infra": 0, "prs_merged": 0}
            ],
        )
        # total_merged > 0 → rule must not fire
        result = _check_direction_health(d)
        assert result is None

    # --- Rule 3: High code failure rate ---

    def test_high_failure_rate_above_threshold_returns_message(self):
        # 3 cycles: 3 tasks each, 3 failed, 0 infra → 9/9 = 100% > 70%
        d = _direction(
            research_history=_history(3, tasks_created=3, tasks_failed=3,
                                      tasks_failed_infra=0, prs_merged=0),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "failure rate" in result.lower()

    def test_high_failure_rate_exactly_at_threshold_not_triggered(self):
        # 10 tasks total, 7 code failures → 70% == threshold, NOT > 70%
        d = _direction(
            research_history=[
                {"tasks_created": 10, "tasks_completed": 3, "tasks_failed": 7,
                 "tasks_failed_infra": 0, "prs_merged": 1},
            ] * 3,
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
                {"tasks_created": 10, "tasks_completed": 4, "tasks_failed": 6,
                 "tasks_failed_infra": 6, "prs_merged": 0},
            ] * 3,
        )
        result = _check_direction_health(d)
        assert result is None

    def test_high_failure_rate_requires_3_plus_cycles(self):
        d = _direction(
            research_history=_history(2, tasks_created=3, tasks_failed=3,
                                      tasks_failed_infra=0, prs_merged=0),
        )
        result = _check_direction_health(d)
        assert result is None

    def test_high_failure_rate_only_uses_last_3_cycles(self):
        # 5 cycles: first 2 have 100% failure; last 3 are healthy
        bad = _history(2, tasks_created=5, tasks_failed=5,
                       tasks_failed_infra=0, prs_merged=0)
        good = _history(3, tasks_created=5, tasks_failed=1,
                        tasks_failed_infra=0, prs_merged=2)
        d = _direction(research_history=bad + good)
        result = _check_direction_health(d)
        assert result is None

    # --- No health issue ---

    def test_healthy_direction_returns_none(self):
        d = _direction(
            possibly_saturated=False,
            research_history=_history(6, tasks_created=3, prs_merged=2,
                                      tasks_failed=0, tasks_failed_infra=0),
        )
        result = _check_direction_health(d)
        assert result is None

    def test_empty_history_returns_none(self):
        result = _check_direction_health(_direction(research_history=[]))
        assert result is None

    def test_missing_research_history_key_returns_none(self):
        result = _check_direction_health({"possibly_saturated": False})
        assert result is None


# ── _is_code_failure_pause_reason ────────────────────────────────────────────

class TestIsCodeFailurePauseReason:

    def test_rule3_high_failure_rate_prefix_classified_as_code_failure(self):
        # Use the actual message _check_direction_health produces for Rule 3
        d = _direction(
            research_history=_history(3, tasks_created=3, tasks_failed=3,
                                      tasks_failed_infra=0, prs_merged=0),
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert _is_code_failure_pause_reason(reason) is True

    def test_rule4_recent_stall_prefix_classified_as_code_failure(self):
        # Rule 4 fires when 3 cycles all have >=1 code failure and 0 merges
        d = _direction(
            research_history=_history(3, tasks_created=2, tasks_completed=1,
                                      tasks_failed=1, tasks_failed_infra=0,
                                      prs_merged=0),
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
            research_history=_history(5, prs_merged=0, tasks_created=2,
                                      tasks_completed=2, tasks_failed=0),
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert _is_code_failure_pause_reason(reason) is False

    def test_none_reason_returns_false(self):
        assert _is_code_failure_pause_reason(None) is False

    def test_empty_string_returns_false(self):
        assert _is_code_failure_pause_reason("") is False


# ── _build_direction_failure_summary ─────────────────────────────────────────

class TestBuildDirectionFailureSummary:

    def test_summarizes_last_3_cycles(self):
        # 5 cycles; only last 3 should be aggregated
        history = (
            _history(2, tasks_created=9, tasks_failed=9, prs_merged=0)
            + _history(3, tasks_created=2, tasks_completed=1, tasks_failed=1,
                       tasks_failed_infra=0, prs_merged=0)
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
            research_history=_history(3, tasks_created=10, tasks_failed=6,
                                      tasks_failed_infra=4, prs_merged=0),
        )
        summary = _build_direction_failure_summary(d)
        assert summary["tasks_failed_total"] == 18
        assert summary["tasks_failed_infra"] == 12
        assert summary["tasks_failed_code"] == 6

    def test_computes_code_failure_rate(self):
        d = _direction(
            research_history=_history(3, tasks_created=4, tasks_failed=3,
                                      tasks_failed_infra=0, prs_merged=0),
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
        history = _history(3, prs_merged=2) + [
            {"tasks_created": 3, "tasks_completed": 1, "tasks_failed": 0,
             "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 3, "tasks_completed": 1, "tasks_failed": 0,
             "tasks_failed_infra": 0, "prs_merged": 0},
        ]
        d = _direction(research_history=history)
        result = _check_direction_completion(d)
        assert result is None

    def test_not_triggered_when_recent_merged_above_threshold(self):
        # Last 2 cycles have 2 merged PRs (> 1)
        history = _history(2, prs_merged=1) + [
            {"tasks_created": 1, "tasks_completed": 1, "tasks_failed": 0,
             "tasks_failed_infra": 0, "prs_merged": 1},
            {"tasks_created": 1, "tasks_completed": 1, "tasks_failed": 0,
             "tasks_failed_infra": 0, "prs_merged": 1},
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
        rejected = [{"text": "migrate authentication service new token system",
                     "rejected_at": recent}]
        assert _is_direction_duplicate(proposal, [], rejected) is True

    def test_old_rejected_proposal_ignored(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        proposal = "migrate authentication service new token system"
        rejected = [{"text": "migrate authentication service new token system",
                     "rejected_at": old}]
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

    def test_retired_direction_tighter_threshold_3_words_triggers_duplicate(self):
        # 3-word overlap against an expired direction should be flagged (tighter threshold = 3)
        # Note: "improve" is a stopword and is excluded from overlap counting
        proposal = "cache database query performance"
        existing = [{"text": "cache database query latency optimization", "status": "expired"}]
        # Non-stopword overlap: cache, database, query = 3 >= threshold 3 for retired → True
        assert _is_direction_duplicate(proposal, existing, []) is True

    def test_active_direction_still_requires_4_words(self):
        # Same 3-word overlap against an active direction should NOT be flagged (threshold 4)
        proposal = "cache database query performance"
        existing = [{"text": "cache database query latency optimization", "status": "active"}]
        # Non-stopword overlap: cache, database, query = 3 < threshold 4 for active → False
        assert _is_direction_duplicate(proposal, existing, []) is False

    def test_superseded_direction_tighter_threshold(self):
        # superseded status also uses the tighter threshold of 3
        proposal = "migrate authentication service token"
        existing = [{"text": "migrate authentication service old token system", "status": "superseded"}]
        # Overlap: migrate, authentication, service, token = 4 >= 3 → True
        assert _is_direction_duplicate(proposal, existing, []) is True

    def test_retired_threshold_2_words_does_not_trigger(self):
        # Only 2 shared non-stopword words even against a retired direction → False
        proposal = "caching performance metrics"
        existing = [{"text": "caching performance strategy deployment", "status": "expired"}]
        # Overlap: caching, performance = 2 < 3 → False
        assert _is_direction_duplicate(proposal, existing, []) is False


# ── _record_strategic_task_failure ───────────────────────────────────────────

@pytest_asyncio.fixture()
async def strat_fail_db(mocker):
    """Isolated in-memory DB for _record_strategic_task_failure tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


async def _make_failure_task(session, project, task_type="strategic_review",
                             context=None, status_message=None):
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
        project = Project(name="P", user_id="u1",
                          vision={"purpose": "test", "_strategic_failures": 2})
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
            id=cycle_id, project_id=project.id,
            outcome=sc.OUTCOME_PHASE2_RUNNING,
            phase1_started_at=_utcnow(),
            phase1_completed_at=_utcnow(),
            phase2_started_at=_utcnow(),
        )
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        task = await _make_failure_task(
            strat_fail_db, project,
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
            id=cycle_id, project_id=project.id,
            outcome=sc.OUTCOME_PHASE1_RUNNING,
            phase1_started_at=_utcnow(),
        )
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        task = await _make_failure_task(
            strat_fail_db, project,
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
            name="P", user_id="u1",
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
            name="P", user_id="u1",
            vision={"purpose": "test",
                    "_strategic_cycle_history": [{"cycle_id": cycle_id}]},
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        long_message = "x" * 500
        task = await _make_failure_task(
            strat_fail_db, project,
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
            id=cycle_id, project_id=project.id,
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
            payloads=[{
                "type": "strategic_review",
                "prompt": "p",
                "failure_category": "strategic_thinker_artifacts_missing",
            }],
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
            id=cycle_id, project_id=project.id,
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
            name="P", user_id="u1",
            vision={"_direction_starvation_count": 3, "_strategic_frequency_override": 2},
            directions=[
                _active_dir(id="1", category="product"),
                _active_dir(id="2", category="product"),
                _active_dir(id="3", category="product"),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(project, dirs_created=0, session=strat_fail_db)

        assert project.vision.get("_direction_starvation_count") == 0
        assert "_strategic_frequency_override" not in project.vision

    @pytest.mark.asyncio
    async def test_dirs_created_resets_counter(self, strat_fail_db, mocker):
        mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
        project = Project(
            name="P", user_id="u1",
            vision={"_direction_starvation_count": 4, "_strategic_frequency_override": 2},
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        # dirs_created > 0 → reset even though has_gaps is True
        await _check_direction_starvation(project, dirs_created=1, session=strat_fail_db)

        assert project.vision.get("_direction_starvation_count") == 0
        assert "_strategic_frequency_override" not in project.vision

    @pytest.mark.asyncio
    async def test_below_minimum_increments_counter(self, strat_fail_db, mocker):
        alert_mock = mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
        project = Project(
            name="P", user_id="u1", vision={},
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(project, dirs_created=0, session=strat_fail_db)

        assert project.vision.get("_direction_starvation_count") == 1
        # Level 1 — no alert, no override
        assert "_strategic_frequency_override" not in project.vision
        alert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_level_2_creates_alert_and_frequency_override(self, strat_fail_db, mocker):
        alert_mock = mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
        project = Project(
            name="P", user_id="u1",
            vision={"_direction_starvation_count": 1},
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(project, dirs_created=0, session=strat_fail_db)

        assert project.vision.get("_direction_starvation_count") == 2
        assert project.vision.get("_strategic_frequency_override") == 2
        alert_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_level_5_auto_creates_no_product_fallback(self, strat_fail_db, mocker):
        alert_mock = mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
        # Active directions but NONE are product → gap_type=NO_PRODUCT_DIRECTIONS
        project = Project(
            name="P", user_id="u1",
            vision={"_direction_starvation_count": 4, "_strategic_frequency_override": 2},
            directions=[
                _active_dir(id="1", category="structural"),
                _active_dir(id="2", category="structural"),
                _active_dir(id="3", category="structural"),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(project, dirs_created=0, session=strat_fail_db)

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
    async def test_level_5_below_minimum_fallback_no_category(self, strat_fail_db, mocker):
        mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
        # 1 active product direction; product_count > 0 but active_count < 3
        project = Project(
            name="P", user_id="u1",
            vision={"_direction_starvation_count": 4},
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(project, dirs_created=0, session=strat_fail_db)

        assert len(project.directions) == 2
        new_dir = project.directions[-1]
        # BELOW_MINIMUM → no category set
        assert "category" not in new_dir

    @pytest.mark.asyncio
    async def test_level_5_all_saturated_fallback(self, strat_fail_db, mocker):
        mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
        # 3 active product directions, all saturated → gap_type=ALL_SATURATED
        project = Project(
            name="P", user_id="u1",
            vision={"_direction_starvation_count": 4},
            directions=[
                _active_dir(id="1", category="product", possibly_saturated=True),
                _active_dir(id="2", category="product", possibly_saturated=True),
                _active_dir(id="3", category="product", possibly_saturated=True),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(project, dirs_created=0, session=strat_fail_db)

        assert len(project.directions) == 4
        new_dir = project.directions[-1]
        assert "category" not in new_dir

    @pytest.mark.asyncio
    async def test_level_5_cap_reached_skips_creation(self, strat_fail_db, mocker):
        alert_mock = mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"max_active_directions": 2},
            clear=False,
        )
        project = Project(
            name="P", user_id="u1",
            vision={"_direction_starvation_count": 4},
            directions=[
                _active_dir(id="1", category="structural"),
                _active_dir(id="2", category="structural"),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(project, dirs_created=0, session=strat_fail_db)

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
            name="P", user_id="u1",
            directions=[
                {"id": "d-a", "notes": [{"text": f"{_CREATED_NOTE_PREFIX}because A"}]},
                {"id": "d-b", "notes": [{"text": f"{_RETIRED_NOTE_PREFIX}because B"}]},
            ],
        )
        created = _collect_rationales(project, ["d-a", "d-missing"], _CREATED_NOTE_PREFIX)
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
            name="P", user_id="u1",
            directions=[
                {
                    "id": "d-n", "status": "active", "text": "new dir",
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
            "direction_id": "d-gone", "role": "retired",
            "status": "missing", "rationale": "stalled for 5 cycles",
        }

    def test_build_previous_decision_outcomes_no_rationales_is_backward_compatible(self):
        project = Project(
            name="P", user_id="u1",
            directions=[{"id": "d-n", "status": "active", "text": "t", "research_history": []}],
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
            "id": "d0", "text": "cache invalidation dashboard refresh",
            "research_history": [{"prs_merged": 2}, {"prs_merged": 1}],
        }
        p = Project(name="P", user_id="u", directions=[similar],
                    vision={"systems": [{"id": "s", "status": "functional"}]})
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
        p = Project(name="P", user_id="u",
                    directions=[{"id": "d1", "text": "x" * 60, "research_history": []}],
                    vision={})
        assert _collect_creation_predictions(p, ["d1", "d-missing"]).keys() == {"d1"}
        assert _collect_retirement_metrics(p, ["d1", "d-missing"]).keys() == {"d1"}

    def test_build_previous_decision_outcomes_includes_prediction_and_delta(self):
        from app.strategic_spawning import _build_previous_decision_outcomes
        project = Project(
            name="P", user_id="u1",
            directions=[{
                "id": "d-n", "status": "active", "text": "new",
                "research_history": [{"prs_merged": 4}],
            }],
            vision={},
        )
        # Audit W2: cycle history is now passed in (was JSONB on vision).
        cycle_history = [{
            "cycle_id": "sc-1",
            "outcome": "completed",
            "created_direction_ids": ["d-n"],
            "retired_direction_ids": ["d-gone"],
            "created_predictions": {"d-n": {
                "estimated_difficulty": "high", "text_length": 3,
                "mature_system_count": 0, "similar_direction_count": 1,
                "similar_past_merges": 1,
            }},
            "retired_final_metrics": {"d-gone": {
                "final_quality_score": 0.2, "final_prs_merged": 0,
                "final_cycles_active": 5,
            }},
        }]
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
async def test_strategic_completion_persists_recommended_mode_on_create(db_session, mocker):
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
        payloads=[{"type": "strategic_review", "prompt": "review",
                   "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json
    logs = json.dumps({
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
    })

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or [])
                if d.get("text") == "Audit the auth module"]
    assert len(new_dirs) == 1
    assert new_dirs[0].get("recommended_mode") == "audit", (
        f"Expected recommended_mode='audit' on created direction; "
        f"got {new_dirs[0].get('recommended_mode')!r}"
    )


@pytest.mark.asyncio
async def test_strategic_completion_persists_recommended_mode_on_update(db_session, mocker):
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
            {"id": "d-upd1", "text": "Existing direction", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review",
                   "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [
            {
                "op": "update",
                "direction_id": "d-upd1",
                "rationale": "drift detected",
                "changes": {"recommended_mode": "fundamentals"},
            },
        ],
    })

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
        payloads=[{"type": "strategic_review", "prompt": "review",
                   "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json
    logs = json.dumps({
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
    })

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("text") == "A direction"]
    assert len(new_dirs) == 1
    # Field should be absent — invalid mode is silently dropped.
    assert "recommended_mode" not in new_dirs[0]


# ── recommended_mode='pause' auto-pause dispatch ─────────────────────────────

@pytest.mark.asyncio
async def test_strategic_completion_pause_mode_auto_pauses_on_create(db_session, mocker):
    """create op with recommended_mode='pause' starts the direction in paused state.

    This makes the existing spawn-skip (status != 'active') mechanism handle
    the 'pause' mode without requiring a separate check in spawn-side code."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{"op": "create", "text": "Paused direction",
                                   "priority": "medium", "recommended_mode": "pause",
                                   "success_evidence": "No blockers remain",
                                   "retirement_condition": "Gap closed for 2 cycles"}],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("text") == "Paused direction"]
    assert len(new_dirs) == 1
    d = new_dirs[0]
    assert d.get("status") == "paused", f"Expected paused, got {d.get('status')!r}"
    assert d.get("auto_paused") is True
    assert d.get("recommended_mode") == "pause"


@pytest.mark.asyncio
async def test_strategic_completion_pause_mode_auto_pauses_on_update(db_session, mocker):
    """update op with recommended_mode='pause' transitions an active direction to paused."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {"id": "d-pauseme", "text": "Active direction", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [
            {"op": "update", "direction_id": "d-pauseme", "changes": {"recommended_mode": "pause"}},
        ],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-pauseme")
    assert target.get("status") == "paused"
    assert target.get("auto_paused") is True
    assert target.get("recommended_mode") == "pause"


@pytest.mark.asyncio
async def test_strategic_completion_non_pause_modes_preserve_active_status(db_session, mocker):
    """update ops with audit/fundamentals/ship modes do not change direction status."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {"id": "d-a", "text": "Audit dir", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
            {"id": "d-f", "text": "Fundamentals dir", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
            {"id": "d-s", "text": "Ship dir", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [
            {"op": "update", "direction_id": "d-a", "changes": {"recommended_mode": "audit"}},
            {"op": "update", "direction_id": "d-f", "changes": {"recommended_mode": "fundamentals"}},
            {"op": "update", "direction_id": "d-s", "changes": {"recommended_mode": "ship"}},
        ],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    by_id = {d["id"]: d for d in (project.directions or [])}
    for did, expected_mode in [("d-a", "audit"), ("d-f", "fundamentals"), ("d-s", "ship")]:
        d = by_id[did]
        assert d.get("status") == "active", f"{did} should stay active for mode={expected_mode}"
        assert d.get("recommended_mode") == expected_mode
        assert not d.get("auto_paused")


# ── Structured direction fields (success_evidence / retirement_condition) ─────


def _make_project_and_task_for_structured_fields(db_session):
    """Helper returning (project, task) for structured-field tests."""
    import asyncio

    async def _build():
        project = Project(
            name="SF", user_id="u1",
            vision={"purpose": "test", "revision": 1},
            directions=[],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
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
        name="SF", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Users can authenticate via SSO",
            "priority": "medium",
            "success_evidence": "Login flow passes end-to-end with SAML provider",
            "retirement_condition": "3 consecutive cycles with 0 auth-related failures",
        }],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if "SSO" in d.get("text", "")]
    assert len(dirs) == 1, "Direction should have been created"
    ctx = dirs[0].get("context") or {}
    assert ctx.get("success_evidence") == "Login flow passes end-to-end with SAML provider"
    assert ctx.get("retirement_condition") == "3 consecutive cycles with 0 auth-related failures"


@pytest.mark.asyncio
async def test_direction_create_rejected_when_success_evidence_missing(db_session, mocker, caplog):
    """A create op missing success_evidence is rejected (direction not created) with a warning log."""
    import json
    import logging
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="SF2", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Improve test coverage",
            "priority": "medium",
            # success_evidence intentionally omitted
            "retirement_condition": "Coverage above 80%",
        }],
    })
    with caplog.at_level(logging.WARNING, logger="app.strategic_completion"):
        await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if "test coverage" in d.get("text", "").lower()]
    assert len(dirs) == 0, "Direction without success_evidence must not be created"
    assert any("success_evidence" in r.message or "retirement_condition" in r.message for r in caplog.records)


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
        name="Legacy", user_id="u1",
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
            {"cycle_id": "sc-1", "outcome": "failed_phase2", "retirement_critiques": [{"direction_id": "d-1", "what_it_addressed": "x", "pattern_that_produced_it": "y", "lesson": "z"}]},
            {"cycle_id": "sc-2", "outcome": "completed", "retirement_critiques": [{"direction_id": "d-2", "what_it_addressed": "a", "pattern_that_produced_it": "b", "lesson": "c"}]},
        ]
        result = _build_past_retirement_critiques(history)
        assert len(result) == 1
        assert result[0]["direction_id"] == "d-2"

    def test_list_critiques_extracted(self):
        critiques = [
            {"direction_id": "d-abc", "what_it_addressed": "CI pipeline speed", "pattern_that_produced_it": "premature optimization", "lesson": "Wait for evidence before optimizing."},
        ]
        history = [{"cycle_id": "sc-1", "outcome": "completed", "retirement_critiques": critiques}]
        result = _build_past_retirement_critiques(history)
        assert len(result) == 1
        assert result[0]["direction_id"] == "d-abc"
        assert result[0]["pattern_that_produced_it"] == "premature optimization"

    def test_no_eligible_sentinel_preserved(self):
        history = [{"cycle_id": "sc-1", "outcome": "completed", "retirement_critiques": {"no_eligible_retirements": True}}]
        result = _build_past_retirement_critiques(history)
        assert result == [{"no_eligible_retirements": True}]

    def test_truncates_to_n(self):
        critiques = [
            {"direction_id": f"d-{i}", "what_it_addressed": "x", "pattern_that_produced_it": "y", "lesson": "z"}
            for i in range(3)
        ]
        history = [
            {"cycle_id": f"sc-{j}", "outcome": "completed", "retirement_critiques": critiques}
            for j in range(4)
        ]
        result = _build_past_retirement_critiques(history, n=5)
        assert len(result) == 5

    def test_default_n_is_5(self):
        critiques = [
            {"direction_id": f"d-{i}", "what_it_addressed": "x", "pattern_that_produced_it": "y", "lesson": "z"}
            for i in range(4)
        ]
        history = [
            {"cycle_id": f"sc-{j}", "outcome": "completed", "retirement_critiques": critiques}
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
        name="RC-API", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[{"id": "d-ret1", "status": "expired", "text": "Old direction", "research_history": []}],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    critiques = [{"direction_id": "d-ret1", "what_it_addressed": "Speed up CI", "pattern_that_produced_it": "premature optimization", "lesson": "Wait for evidence."}]
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
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
    assert "retirement_critiques" in meta, "retirement_critiques must be persisted in cycle_metadata"
    assert meta["retirement_critiques"] == critiques


@pytest.mark.asyncio
async def test_retirement_critiques_persisted_via_fallback_path(db_session, mocker):
    """Fallback (log-parse) path: retired_direction_critiques from JSON output ends up in cycle metadata."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RC-FB", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    critiques = [{"direction_id": "d-old1", "what_it_addressed": "Caching layer", "pattern_that_produced_it": "scope drift", "lesson": "Keep scope tight."}]
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [],
        "retired_direction_critiques": critiques,
    })
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert "retirement_critiques" in meta, "retirement_critiques must be persisted via fallback path"
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
        name="RC-NE", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [],
        "retired_direction_critiques": {"no_eligible_retirements": True},
    })
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
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
async def test_dedup_reasoning_persisted_on_successful_create_fallback_path(db_session, mocker):
    """dedup_reasoning dict is stored in direction.context on a successful fallback-path create."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="DR1", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Migrate auth service to OAuth2",
            "priority": "medium",
            "success_evidence": "All auth tests pass",
            "retirement_condition": "OAuth2 flow live for 3 cycles",
            "dedup_reasoning": {
                "closest_directions": ["Improve login flow", "Add SSO support"],
                "distinction": "Targets the auth backend protocol, not the UI flow",
            },
        }],
    })
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
        name="DR2", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Reduce p99 API latency",
            "priority": "high",
            "success_evidence": "p99 < 200ms",
            "retirement_condition": "3 cycles below target",
        }],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if "latency" in d.get("text", "").lower()]
    assert len(dirs) == 1, "Direction should have been created without dedup_reasoning"
    ctx = dirs[0].get("context") or {}
    assert "dedup_reasoning" not in ctx


@pytest.mark.asyncio
async def test_dedup_rejected_with_dedup_reasoning_emits_warning_fallback(db_session, mocker, caplog):
    """When a duplicate is rejected AND dedup_reasoning.distinction was supplied, a structured warning is logged."""
    import json
    import logging
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
        name="DR3", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[existing_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Migrate auth service to OAuth2",
            "priority": "medium",
            "success_evidence": "All auth tests pass",
            "retirement_condition": "OAuth2 flow live for 3 cycles",
            "dedup_reasoning": {
                "closest_directions": ["d-existing1"],
                "distinction": "This one focuses on the token refresh path specifically",
            },
        }],
    })
    with caplog.at_level(logging.WARNING, logger="app.strategic_completion"):
        await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if d.get("id") != "d-existing1"]
    assert len(dirs) == 0, "Duplicate should have been rejected"
    assert any("STRATEGIC_DEDUP_DISAGREEMENT" in r.message for r in caplog.records), (
        "Warning with STRATEGIC_DEDUP_DISAGREEMENT should have been emitted"
    )


@pytest.mark.asyncio
async def test_dedup_reasoning_fields_capped_at_max_length(db_session, mocker):
    """closest_directions is capped at 3 items; distinction is capped at 500 chars."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="DR4", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    long_distinction = "x" * 600
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Consolidate logging infrastructure",
            "priority": "low",
            "success_evidence": "Single log sink used everywhere",
            "retirement_condition": "All services use unified sink for 2 cycles",
            "dedup_reasoning": {
                "closest_directions": ["dir-a", "dir-b", "dir-c", "dir-d", "dir-e"],
                "distinction": long_distinction,
            },
        }],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if "logging" in d.get("text", "").lower()]
    assert len(dirs) == 1
    ctx = dirs[0].get("context") or {}
    dr = ctx.get("dedup_reasoning") or {}
    assert len(dr["closest_directions"]) == 3, "closest_directions should be capped at 3"
    assert len(dr["distinction"]) == 500, "distinction should be capped at 500 chars"


# ── Semantic dedup on direction creates ──────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_dedup_blocks_paraphrased_active_direction_fallback(db_session, mocker):
    """Fallback path: paraphrased active direction is blocked by semantic dedup."""
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
        name="SD1", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[existing_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Paraphrase of the existing direction (4+ non-stopword words overlap)
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Transition authentication service OAuth2 token implementation",
            "priority": "medium",
            "success_evidence": "All auth flows use OAuth2",
            "retirement_condition": "OAuth2 live for 3 cycles",
        }],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-active1"]
    assert len(new_dirs) == 0, "Paraphrased duplicate should have been blocked"


@pytest.mark.asyncio
async def test_semantic_dedup_blocks_paraphrased_retired_direction_fallback(db_session, mocker):
    """Fallback path: paraphrased retired direction is blocked by tighter (3-word) threshold."""
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
        name="SD2", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[retired_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Paraphrase with 3 overlapping non-stopword words (tighter retired threshold)
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Consolidate logging pipeline distributed system",
            "priority": "low",
            "success_evidence": "Single log pipeline in place",
            "retirement_condition": "Pipeline stable for 2 cycles",
        }],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-ret1"]
    assert len(new_dirs) == 0, "Paraphrased retired duplicate should have been blocked"


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
        name="SD3", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[existing_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Genuinely distinct — no meaningful word overlap with existing
    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [{
            "op": "create",
            "text": "Reduce database query latency via connection pooling",
            "priority": "high",
            "success_evidence": "p99 query time below 50ms",
            "retirement_condition": "Latency stable for 2 cycles",
        }],
    })
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-existing2"]
    assert len(new_dirs) == 1, "Distinct direction should have been created"


# ── set_retirement_critiques HTTP API path ─────────────────────────────────────


@pytest.mark.asyncio
async def test_set_retirement_critiques_list_payload_via_http(client, db_session, mocker):
    """HTTP API: POST /api/agent/strategic with a list payload returns 200 and stores critiques."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="RC-HTTP", user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[{"id": "d-ret2", "status": "expired", "text": "Old dir", "research_history": []}],
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
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
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
        json={"task_id": str(task.id), "op": "set_retirement_critiques", "data": critiques},
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
        name="RC-HTTP-SENT", user_id="u1",
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
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-rc-sent", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={"task_id": str(task.id), "op": "set_retirement_critiques", "data": {"no_eligible_retirements": True}},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0

    await db_session.refresh(task)
    stored = (task.context or {}).get("_strategic_retirement_critiques")
    assert stored == {"no_eligible_retirements": True}


@pytest.mark.asyncio
async def test_set_retirement_critiques_invalid_dict_returns_400(client, db_session, mocker):
    """HTTP API: a non-sentinel dict payload returns 400."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="RC-HTTP-BAD", user_id="u1",
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
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-rc-bad", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={"task_id": str(task.id), "op": "set_retirement_critiques", "data": {"some": "dict"}},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text


# ── superseded status + successor linkage ─────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_completion_fallback_superseded_update_persists_superseded_by(db_session, mocker):
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
            {"id": "d-old", "text": "Old direction", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps({
        "vision": {"purpose": "test", "revision": 2},
        "direction_operations": [
            {
                "op": "update",
                "direction_id": "d-old",
                "rationale": "superseded by newer approach",
                "changes": {
                    "status": "superseded",
                    "superseded_by": {"direction_id": "d-new", "set_at": "2026-01-01T00:00:00"},
                },
            },
        ],
    })

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-old")
    assert target.get("status") == "superseded", f"Expected status=superseded, got: {target.get('status')}"
    assert target.get("superseded_by") == {"direction_id": "d-new", "set_at": "2026-01-01T00:00:00"}, (
        f"Expected superseded_by to be persisted, got: {target.get('superseded_by')}"
    )


@pytest.mark.asyncio
async def test_strategic_completion_fallback_superseded_derives_superseded_by_from_successor_id(db_session, mocker):
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
            {"id": "d-legacy", "text": "Legacy direction", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps({
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
    })

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-legacy")
    assert target.get("status") == "superseded"
    sb = target.get("superseded_by")
    assert sb is not None, "superseded_by must be derived from successor_id"
    assert sb.get("direction_id") == "d-successor"
    assert "set_at" in sb


@pytest.mark.asyncio
async def test_strategic_http_update_direction_superseded_persists_superseded_by(client, db_session, mocker):
    """HTTP update_direction with status=superseded persists superseded_by and returns 200."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="SUP-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {"id": "d-http-old", "text": "HTTP old direction", "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
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
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-sup-http", task_type="strategic_review")
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
                    "superseded_by": {"direction_id": "d-http-new", "set_at": "2026-01-01T00:00:00"},
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-http-old")
    assert target.get("status") == "superseded"
    assert target.get("superseded_by") == {"direction_id": "d-http-new", "set_at": "2026-01-01T00:00:00"}


# ── _strategic_dedup_rejections persistence ───────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_http_create_direction_dedup_persists_rejection_record(client, db_session, mocker):
    """HTTP create_direction on a semantic duplicate appends a structured record to
    task.context['_strategic_dedup_rejections'] and still returns 409."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    existing_text = "Migrate authentication service to OAuth2 tokens"
    project = Project(
        name="DEDUP-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {"id": "d-exist", "text": existing_text, "status": "active",
             "priority": "medium", "notes": [], "research_history": []},
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
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
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
    assert resp.status_code == 409, resp.text

    await db_session.refresh(task)
    ctx = task.context or {}
    rejections = ctx.get("_strategic_dedup_rejections")
    assert isinstance(rejections, list) and len(rejections) == 1, (
        f"Expected 1 rejection record, got: {rejections}"
    )
    record = rejections[0]
    assert record.get("matched_direction_id") == "d-exist"
    assert existing_text in record.get("rejected_text", "")
    assert record.get("dedup_reasoning") == "slightly different framing"
    assert "rejected_at" in record
