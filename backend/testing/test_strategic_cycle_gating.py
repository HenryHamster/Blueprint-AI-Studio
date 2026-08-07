"""
Tests for the no-delta strategic cycle gate (``strategic_cycle_gating.py``).

A strategic cycle assembles and re-sends the full strategic context (~137 KB
across ~17 turns) whether or not anything changed since the previous cycle.
For a quiet project that costs the full price for no new information. The gate
is a cheap pre-check that runs BEFORE context assembly and short-circuits the
cycle when there is no meaningful delta.

These tests cover both required directions:

  * a genuinely empty delta short-circuits before context assembly — the spawn
    functions are never called and the skip is recorded on vision so it is
    visible and countable;
  * each meaningful signal (new merge / new operator message / version-phase
    change) forces a full cycle.

Plus the conservative fail-closed guarantees: a project with no prior cycle
(bootstrap) or an unanswered (open) operator message runs even with zero
delta, and a skip never advances the watermark so a delta that arrives later
is still caught on the next tick.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.autonomous_research import _spawn_research_task
from app.models import (
    MessageType,
    Project,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
)
from app.release_models import ReleaseVersion
from app.strategic_cycle_gating import (
    compute_cycle_delta_signals,
    latest_cycle_watermark,
    must_run_reasons,
    record_no_delta_skip,
    should_skip_strategic_cycle,
)
from app.time_utils import _utcnow

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user."""
    import app.shared_state as _shared_state
    from app.max_plan_pool import _account_by_id, _max_plan_pools

    uid = "test-user-123"
    entry = {
        "id": "test-acct",
        "user_id": uid,
        "name": "test",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        "total_tasks": 0,
        "token_created_at": None,
        "claude_config_json": None,
    }
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct"] = entry
    _shared_state.deploy_monitoring["task_results"].clear()
    yield
    _max_plan_pools.get(uid, [])[:] = [
        a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct"
    ]
    _account_by_id.pop("test-acct", None)


def _active_direction(direction_id: str, text: str = "Test direction") -> dict:
    return {
        "id": direction_id,
        "text": text,
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }


def _gate_project(**overrides) -> Project:
    """A project that reaches the strategic cadence branch via the modulo
    counter (research_spawn_count 2, frequency override 2) without tripping
    any force_strategic path."""
    base = {
        "name": "Gate Project",
        "user_id": "test-user-123",
        "autonomous_enabled": True,
        "directions": [_active_direction("d-gate01", "Gate direction")],
        "vision": {
            "purpose": "test",
            "_research_spawn_count": 2,
            "_strategic_frequency_override": 2,
        },
        # Has systems, so an architecture cycle is NOT triggered on cycle 1 —
        # the strategy-only spawn (the full context assembly) is what runs.
        "design_doc": {"systems/api": {"id": "api", "status": "working"}},
    }
    base.update(overrides)
    return Project(**base)


async def _add_completed_cycle(
    db_session, project_id, *, started_at, outcome: str = "completed"
) -> StrategicCycle:
    """Insert a StrategicCycle row establishing the watermark."""
    cycle = StrategicCycle(
        id=uuid.uuid4().hex,
        project_id=project_id,
        outcome=outcome,
        phase1_started_at=started_at,
    )
    db_session.add(cycle)
    await db_session.commit()
    return cycle


def _mock_strategic_spawns(mocker):
    return (
        mocker.patch(
            "app.strategic_spawning._spawn_design_doc_update",
            new=AsyncMock(return_value=True),
        ),
        mocker.patch(
            "app.strategic_spawning._spawn_strategic_review",
            new=AsyncMock(return_value=True),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Required direction (a): a genuinely empty delta short-circuits before the
# expensive context assembly — no spawn function is ever called, and the skip
# is recorded on vision so it is visible and countable.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_delta_short_circuits_before_context_assembly(db_session, mocker):
    design_doc_mock, strategic_review_mock = _mock_strategic_spawns(mocker)
    project = _gate_project(name="Quiet Project")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A completed cycle two hours ago establishes the watermark; nothing has
    # happened since (no merges, completions, failures, messages, or version
    # lifecycle movement).
    await _add_completed_cycle(
        db_session, project.id, started_at=_utcnow() - timedelta(hours=2)
    )

    await _spawn_research_task(project, db_session)

    assert not design_doc_mock.called, "no-delta must not spawn architecture"
    assert not strategic_review_mock.called, (
        "no-delta must exit before the full strategic context assembly"
    )
    await db_session.refresh(project)
    vision = project.vision or {}
    assert vision.get("_strategic_no_delta_skip_count") == 1, (
        "the skip must be recorded on vision so it is visible and countable"
    )
    skips = vision.get("_strategic_no_delta_skips") or []
    assert len(skips) == 1
    assert skips[0]["watermark"] is not None
    assert skips[0]["signals"]["new_merges"] == 0
    assert skips[0]["signals"]["new_user_messages"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Required direction (b): each meaningful signal forces a full cycle.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_merge_since_watermark_forces_full_cycle(db_session, mocker):
    design_doc_mock, strategic_review_mock = _mock_strategic_spawns(mocker)
    project = _gate_project(name="Merge Landed")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_completed_cycle(
        db_session, project.id, started_at=_utcnow() - timedelta(hours=2)
    )
    # A PR merged after the watermark.
    db_session.add(
        Task(
            project_id=project.id,
            title="Merged fix",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            completed_at=_utcnow() - timedelta(minutes=30),
            context={"landed_on_integration": True},
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "a merge since the watermark must force the full strategic cycle"
    )
    assert not design_doc_mock.called  # design_doc has systems → strategy-only
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_no_delta_skip_count") is None


@pytest.mark.asyncio
async def test_new_operator_message_since_watermark_forces_full_cycle(
    db_session, mocker
):
    design_doc_mock, strategic_review_mock = _mock_strategic_spawns(mocker)
    project = _gate_project(name="Operator Steered")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_completed_cycle(
        db_session, project.id, started_at=_utcnow() - timedelta(hours=2)
    )
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.NOTE,
            content="Please focus on the auth bug",
            status="open",
            created_at=_utcnow() - timedelta(minutes=10),
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "an operator message since the watermark must force the full strategic cycle"
    )
    assert not design_doc_mock.called
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_no_delta_skip_count") is None


@pytest.mark.asyncio
async def test_version_phase_change_forces_full_cycle(db_session, mocker):
    design_doc_mock, strategic_review_mock = _mock_strategic_spawns(mocker)
    project = _gate_project(name="Version Stabilizing")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_completed_cycle(
        db_session, project.id, started_at=_utcnow() - timedelta(hours=2)
    )
    # Version opened BEFORE the watermark, but its phase changed after it.
    db_session.add(
        ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
            opened_at=_utcnow() - timedelta(hours=3),
            phase_changed_at=_utcnow() - timedelta(minutes=10),
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called, (
        "a release-version phase change since the watermark must force the full "
        "strategic cycle"
    )
    assert not design_doc_mock.called
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_no_delta_skip_count") is None


# ─────────────────────────────────────────────────────────────────────────────
# Fail-closed must-run conditions: even with zero delta, ambiguous/structural
# states RUN rather than risk silently skipping real strategic work.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_prior_cycle_is_bootstrap_must_run(db_session, mocker):
    design_doc_mock, strategic_review_mock = _mock_strategic_spawns(mocker)
    project = _gate_project(name="Bootstrap")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # NOTE: no StrategicCycle row → watermark is None → bootstrap must-run.

    await _spawn_research_task(project, db_session)

    assert design_doc_mock.called or strategic_review_mock.called, (
        "a project with no prior cycle must run (bootstrap)"
    )
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_no_delta_skip_count") is None


@pytest.mark.asyncio
async def test_open_operator_message_runs_even_with_no_new_delta(db_session, mocker):
    design_doc_mock, strategic_review_mock = _mock_strategic_spawns(mocker)
    project = _gate_project(name="Unanswered Note")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _add_completed_cycle(
        db_session, project.id, started_at=_utcnow() - timedelta(hours=2)
    )
    # A message created BEFORE the watermark (so it is NOT a new-delta signal)
    # but still open — the strategist payload feeds on open messages, so
    # unanswered operator intent must force a cycle.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.NOTE,
            content="Help with X",
            status="open",
            created_at=_utcnow() - timedelta(hours=5),
        )
    )
    await db_session.commit()

    await _spawn_research_task(project, db_session)

    assert strategic_review_mock.called or design_doc_mock.called, (
        "an open operator message must force a cycle even with zero new delta"
    )
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_no_delta_skip_count") is None


# ─────────────────────────────────────────────────────────────────────────────
# Watermark semantics: derived from the StrategicCycle table, never advanced by
# a skip, so a delta that arrives mid-check is caught on the next tick.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_never_advances_watermark_and_later_delta_is_caught(db_session):
    project = _gate_project(name="Watermark Stability")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    watermark = _utcnow() - timedelta(hours=2)
    await _add_completed_cycle(db_session, project.id, started_at=watermark)

    # First tick: nothing changed since the watermark → skip, and record it.
    decision1 = await should_skip_strategic_cycle(db_session, project)
    assert decision1.skip
    await record_no_delta_skip(db_session, project.id, decision1)
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_no_delta_skip_count") == 1

    # The watermark is DERIVED from the StrategicCycle table — the recorded
    # skip must not have advanced it.
    watermark2, _ = await latest_cycle_watermark(db_session, project.id)
    assert watermark2 == watermark

    # An operator message arrives after the skipped tick.
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.NOTE,
            content="New request",
            status="open",
            created_at=_utcnow(),
        )
    )
    await db_session.commit()

    decision2 = await should_skip_strategic_cycle(db_session, project)
    assert not decision2.skip, (
        "a delta that arrives after a skip must still be caught on the next tick"
    )
    assert decision2.signals.new_user_messages == 1


# ─────────────────────────────────────────────────────────────────────────────
# Signal hygiene: the strategist's own task completions are the cycle itself
# and must not read as "new work" for the next check; genuine non-strategic
# completions must.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_task_completion_is_not_a_delta(db_session):
    project = _gate_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    watermark = _utcnow() - timedelta(hours=2)
    await _add_completed_cycle(db_session, project.id, started_at=watermark)
    # A design_doc_update completed after the watermark — this IS the cycle.
    db_session.add(
        Task(
            project_id=project.id,
            title="Phase 1 architecture",
            payloads=[{"type": "design_doc_update"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            completed_at=_utcnow() - timedelta(minutes=30),
        )
    )
    await db_session.commit()

    signals = await compute_cycle_delta_signals(db_session, project.id, watermark)
    assert signals.new_completions == 0
    assert signals.new_failures == 0
    assert not signals.has_delta


@pytest.mark.asyncio
async def test_non_strategic_completion_is_a_delta(db_session):
    project = _gate_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    watermark = _utcnow() - timedelta(hours=2)
    await _add_completed_cycle(db_session, project.id, started_at=watermark)
    db_session.add(
        Task(
            project_id=project.id,
            title="Research done",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            completed_at=_utcnow() - timedelta(minutes=30),
        )
    )
    await db_session.commit()

    signals = await compute_cycle_delta_signals(db_session, project.id, watermark)
    assert signals.new_completions == 1
    assert signals.has_delta


# ─────────────────────────────────────────────────────────────────────────────
# Pure-logic checks on the fail-closed must-run matrix.
# ─────────────────────────────────────────────────────────────────────────────


def test_must_run_reasons_fail_closed():
    # No prior cycle + no active directions → bootstrap + no-active-directions.
    project = Project(directions=[], vision={"purpose": "test"})
    reasons = must_run_reasons(project, None, 0, {"purpose": "test"})
    assert "bootstrap:no_prior_cycle" in reasons
    assert "no_active_directions" in reasons

    # A healthy project with a completed prior cycle and an active direction
    # has NO must-run reasons — the delta signals decide.
    healthy = Project(
        directions=[_active_direction("d-ok01")], vision={"purpose": "test"}
    )
    reasons = must_run_reasons(healthy, "completed", 0, {"purpose": "test"})
    assert reasons == []

    # A prior-cycle failure is a recovery decision, never routine.
    reasons = must_run_reasons(healthy, "failed_phase2", 0, {"purpose": "test"})
    assert "prior_cycle_failed" in reasons

    # An open operator message forces a cycle regardless of when it arrived.
    reasons = must_run_reasons(healthy, "completed", 1, {"purpose": "test"})
    assert "open_operator_message" in reasons

    # A pending strategic-refresh marker must run.
    reasons = must_run_reasons(
        healthy,
        "completed",
        0,
        {"purpose": "test", "_strategic_refresh": {"reason": "operator_steering"}},
    )
    assert "pending_strategic_refresh" in reasons

    # All active directions saturated → the strategist must re-evaluate.
    saturated = Project(
        directions=[
            {**_active_direction("d-sat01"), "possibly_saturated": True}
        ],
        vision={"purpose": "test"},
    )
    reasons = must_run_reasons(saturated, "completed", 0, {"purpose": "test"})
    assert "all_directions_saturated" in reasons
