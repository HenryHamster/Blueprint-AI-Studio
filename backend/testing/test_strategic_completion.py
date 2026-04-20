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

from app.strategic_completion import (
    _check_direction_health,
    _check_direction_completion,
    _is_direction_duplicate,
    _record_strategic_task_failure,
    _check_direction_starvation,
    _is_code_failure_pause_reason,
    _build_direction_failure_summary,
    _collect_rationales,
    _extract_cycle_reasoning,
    _extract_rationale_from_notes,
    _CREATED_NOTE_PREFIX,
    _RETIRED_NOTE_PREFIX,
)
from app.strategic_spawning import _build_previous_decision_outcomes
from app.models import Project, Task, TaskStatus


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
        history = _history(3, prs_merged=2) + _history(0)  # just 3 cycles
        d = _direction(research_history=_history(3, prs_merged=2))
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
        cycle_id = "cycle-fail-001"
        project = Project(
            name="P", user_id="u1",
            vision={
                "purpose": "test",
                "_strategic_cycle_history": [
                    {"cycle_id": cycle_id, "phase1_completed_at": "2026-01-01T00:00:00"}
                ],
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        task = await _make_failure_task(
            strat_fail_db, project,
            task_type="strategic_review",
            context={"strategic_cycle_id": cycle_id},
            status_message="Claude API timeout",
        )
        await _record_strategic_task_failure(task, strat_fail_db)

        history = project.vision.get("_strategic_cycle_history", [])
        entry = next((e for e in history if e.get("cycle_id") == cycle_id), None)
        assert entry is not None
        assert entry.get("outcome") == "failed"
        assert entry.get("failed_phase") == "phase2"  # strategic_review → phase2
        assert entry.get("phase2_completed_at") is not None

    @pytest.mark.asyncio
    async def test_updates_cycle_history_for_design_doc_update(self, strat_fail_db):
        cycle_id = "cycle-fail-002"
        project = Project(
            name="P", user_id="u1",
            vision={
                "purpose": "test",
                "_strategic_cycle_history": [
                    {"cycle_id": cycle_id},
                ],
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        task = await _make_failure_task(
            strat_fail_db, project,
            task_type="design_doc_update",
            context={"strategic_cycle_id": cycle_id},
            status_message="VM crash",
        )
        await _record_strategic_task_failure(task, strat_fail_db)

        history = project.vision.get("_strategic_cycle_history", [])
        entry = next((e for e in history if e.get("cycle_id") == cycle_id), None)
        assert entry is not None
        assert entry.get("failed_phase") == "phase1"  # design_doc_update → phase1
        assert entry.get("phase1_completed_at") is not None

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
        project = Project(
            name="P", user_id="u1",
            directions=[
                {
                    "id": "d-n", "status": "active", "text": "new dir",
                    "research_history": [{"prs_merged": 1}],
                },
            ],
            vision={
                "_strategic_cycle_history": [
                    {
                        "cycle_id": "sc-1",
                        "outcome": "completed",
                        "created_direction_ids": ["d-n"],
                        "retired_direction_ids": ["d-gone"],
                        "created_rationales": {"d-n": "because new capability"},
                        "retired_rationales": {"d-gone": "stalled for 5 cycles"},
                        "cycle_reasoning": "focus on velocity",
                    },
                ],
            },
        )
        out = _build_previous_decision_outcomes(project)
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
            vision={
                "_strategic_cycle_history": [
                    {
                        "cycle_id": "sc-1",
                        "outcome": "completed",
                        "created_direction_ids": ["d-n"],
                    },
                ],
            },
        )
        out = _build_previous_decision_outcomes(project)
        assert out is not None
        assert "cycle_reasoning" not in out
        assert "rationale" not in out["created"][0]
