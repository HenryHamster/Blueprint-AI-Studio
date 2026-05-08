"""Tests for the pipeline-regression dispatcher.

Covers:
  (a) dedup against in-flight investigations sharing a regression_class
  (b) cooldown — a spawn within 6h of the prior spawn is skipped
  (c) threshold gating for sample-size and trend-delta floors
  (d) payload shape — investigate type, context fields, prompt content
  (e) end-to-end simulation through _handle_investigation_completion
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.models import DispatcherCycleRecord, Task, TaskStatus
from app.pipeline_regression_dispatcher import (
    _REGRESSION_COOLDOWN,
    _WATCHDOG_ACTIVE_STALE_THRESHOLD,
    _WATCHDOG_MERGED_STALE_THRESHOLD,
    get_dispatcher_cycle_records,
    get_regression_mtta_data,
    run_dispatcher_watchdog_alarms,
    run_once,
    run_recovery_once,
    run_watchdog_once,
)
from app.time_utils import _utcnow

from testing.factories import make_project, make_task, make_task_history


def _design_doc_with_sr_regression(
    *,
    category: str = "correctness",
    prior: float = 0.80,
    current: float = 0.50,
) -> dict:
    """Synthesize a sr_catch_rate_trends snapshot the dispatcher will trip on.

    The dispatcher reads ``project.design_doc["sr_catch_rate_trends"]["categories"]``
    and trips when current_hit_rate dropped vs previous_hit_rate by ≥ 5pp.
    """
    return {
        "sr_catch_rate_trends": {
            "categories": {
                category: {
                    "current_hit_rate": current,
                    "previous_hit_rate": prior,
                    "current_human_count": 12,
                    "trend": "decreasing",
                },
            },
        },
    }


def _make_fix_loop_divergence_task(
    project_id,
    *,
    error_tail: str,
    related_files: list[str],
    scope: str = "small",
) -> Task:
    now = _utcnow()
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=1),
        context={"scope": scope, "related_files": related_files},
        fix_loop_history={
            "lint": [
                {
                    "loop": "lint",
                    "iteration": 1,
                    "error_tail": error_tail,
                    "exit_reason": "divergence_detected",
                    "started_at": (now - timedelta(minutes=2)).isoformat(),
                    "ended_at": now.isoformat(),
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_payload_shape_and_context(db_session):
    """Spawned investigation has the expected payload + context fields (case d)."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    new_task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert new_task is not None
    payload = new_task.first_payload or {}
    assert payload.get("type") == "investigate"
    assert payload.get("base_branch") == "main"
    assert payload.get("effort") == "low"
    assert "Pipeline regression detected" in payload.get("prompt", "")
    assert "Prior rate: 80.0%" in payload["prompt"]
    assert "Current rate: 50.0%" in payload["prompt"]

    ctx = new_task.context or {}
    assert ctx["spawned_by"] == "pipeline_regression_dispatcher"
    assert ctx["regression_class"] == "sr_catch_rate"
    assert ctx["signal_name"] == "sr_catch_rate"
    assert ctx["prior_rate"] == 0.80
    assert ctx["current_rate"] == 0.50
    assert ctx["sample_size"] >= 5
    assert "dispatcher_cooldown_until_iso" in ctx


@pytest.mark.asyncio
async def test_dedup_inflight_blocks_second_spawn(db_session):
    """A second call with an in-flight investigation does NOT double-spawn (case a)."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await run_once(db_session, project.id)
    assert first == 1

    # Confirm the spawned task is in QUEUED (in-flight). Run again — should
    # now hit dedup-hit, not spawn a second.
    second = await run_once(db_session, project.id)
    assert second == 0

    rows = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string() == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_cooldown_blocks_after_terminal(db_session):
    """A spawn within 6h of the prior spawn is skipped even after the prior one terminates (case b)."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    # Mark the spawned task COMPLETED — no longer in-flight, but cooldown
    # window has not elapsed yet, so the next call MUST still skip.
    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    second = await run_once(db_session, project.id)
    assert second == 0

    # Now backdate the spawn beyond the cooldown — a fresh spawn should fire.
    task.created_at = _utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5)
    db_session.add(task)
    await db_session.commit()

    third = await run_once(db_session, project.id)
    assert third == 1


@pytest.mark.asyncio
async def test_threshold_gating_below_delta_does_not_spawn(db_session):
    """Signal not meeting the 5pp delta floor produces no spawn (case c)."""
    # Drop is only 2pp — below _TREND_DELTA_THRESHOLD of 5pp.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.78,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0
    rows = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_threshold_gating_no_trends_block(db_session):
    """No sr_catch_rate_trends key in design_doc → no spawn (case c, secondary)."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_per_direction_sr_catch_rate_spawns_when_project_wide_healthy(db_session):
    """Per-direction regression spawns a direction-scoped investigation when project-wide is stable."""
    # Project-wide correctness is stable (no regression).
    # Direction "dir-test1" has correctness hit_rate well below the project-wide baseline.
    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.80,
                    "previous_hit_rate": 0.82,  # delta=0.02, below threshold
                    "current_human_count": 20,
                    "trend": "stable",
                },
            },
            "by_direction": {
                "dir-test1": {
                    "sample_count": 5,
                    "miss_rate": 0.40,
                    "categories": {
                        "correctness": {
                            "hit_rate": 0.60,  # baseline(0.82) - 0.60 = 0.22 >= threshold
                            "miss_rate": 0.40,
                            "sample_count": 5,
                        }
                    },
                }
            },
        }
    }
    project = make_project(design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    ctx = task.context or {}
    assert ctx["regression_class"] == "sr_catch_rate:correctness:dir-test1"
    assert ctx["direction_id"] == "dir-test1"
    payload = task.first_payload or {}
    assert "dir-test1" in payload.get("prompt", "")


@pytest.mark.asyncio
async def test_per_direction_sr_does_not_double_spawn_when_project_wide_trips(
    db_session,
):
    """When project-wide category trips, per-direction signal for same category is suppressed."""
    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.50,
                    "previous_hit_rate": 0.80,  # delta=0.30, project-wide trips
                    "current_human_count": 12,
                    "trend": "decreasing",
                },
            },
            "by_direction": {
                "dir-test2": {
                    "sample_count": 5,
                    "miss_rate": 0.50,
                    "categories": {
                        "correctness": {
                            "hit_rate": 0.50,
                            "miss_rate": 0.50,
                            "sample_count": 5,
                        }
                    },
                }
            },
        }
    }
    project = make_project(design_doc=design_doc)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1  # exactly one spawn total

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    ctx = task.context or {}
    # Project-wide fires; per-direction is suppressed for the same category.
    assert ctx["regression_class"] == "sr_catch_rate"


@pytest.mark.asyncio
async def test_no_spawn_when_caps_exhausted(db_session):
    """If max_total_research_per_project is already saturated, dispatcher skips."""
    import app.shared_state as _shared_state

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Saturate the project's research budget with existing investigate tasks.
    original_max = _shared_state.runtime_config.get(
        "max_total_research_per_project",
        5,
    )
    _shared_state.runtime_config["max_total_research_per_project"] = 1
    blocker = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate", "prompt": "x", "base_branch": "main"}],
        context={"direction_id": "some-dir"},
    )
    db_session.add(blocker)
    await db_session.commit()
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 0
    finally:
        _shared_state.runtime_config["max_total_research_per_project"] = original_max


@pytest.mark.asyncio
async def test_fix_loop_divergence_fingerprint_trips_on_same_fingerprint(db_session):
    import app.shared_state as _shared_state

    original = _shared_state.runtime_config.get(
        "fix_loop_divergence_fingerprint_enabled", False
    )
    _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = True
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    for tail in (
        "TypeError: build failed for job 123 size 10MB",
        "TypeError: build failed for job 456 size 11MB",
        "TypeError: build failed for job 789 size 12MB",
    ):
        db_session.add(
            _make_fix_loop_divergence_task(
                project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]
            )
        )
    await db_session.commit()
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1
        investigate = (
            (
                await db_session.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.context["regression_class"]
                        .as_string()
                        .startswith("fix_loop_divergence_fingerprint:"),
                    )
                )
            )
            .scalars()
            .first()
        )
        assert investigate is not None
        prompt = (investigate.first_payload or {})["prompt"]
        assert "Normalized fingerprint:" in prompt
        assert "Sample failure tail:" in prompt
        assert "Affected scope(s): small" in prompt
        assert "backend/app/poisoned.py" in prompt
    finally:
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = (
            original
        )


@pytest.mark.asyncio
async def test_fix_loop_divergence_fingerprint_different_fingerprints_do_not_trip(
    db_session,
):
    import app.shared_state as _shared_state

    original = _shared_state.runtime_config.get(
        "fix_loop_divergence_fingerprint_enabled", False
    )
    _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = True
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    for tail in (
        "AssertionError: missing foo",
        "AssertionError: missing bar",
        "RuntimeError: missing baz",
    ):
        db_session.add(
            _make_fix_loop_divergence_task(
                project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]
            )
        )
    await db_session.commit()
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 0
    finally:
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = (
            original
        )


@pytest.mark.asyncio
async def test_fix_loop_divergence_fingerprint_cooldown_blocks_repeat_spawn(db_session):
    import app.shared_state as _shared_state

    original = _shared_state.runtime_config.get(
        "fix_loop_divergence_fingerprint_enabled", False
    )
    _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = True
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    for tail in (
        "TypeError: build failed for job 123 size 10MB",
        "TypeError: build failed for job 456 size 11MB",
        "TypeError: build failed for job 789 size 12MB",
    ):
        db_session.add(
            _make_fix_loop_divergence_task(
                project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]
            )
        )
    await db_session.commit()
    try:
        first = await run_once(db_session, project.id)
        assert first == 1
        investigate = (
            (
                await db_session.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.context["regression_class"]
                        .as_string()
                        .startswith("fix_loop_divergence_fingerprint:"),
                    )
                )
            )
            .scalars()
            .first()
        )
        assert investigate is not None
        investigate.status = TaskStatus.COMPLETED
        db_session.add(investigate)
        await db_session.commit()
        second = await run_once(db_session, project.id)
        assert second == 0
        investigate.created_at = _utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5)
        db_session.add(investigate)
        await db_session.commit()
        third = await run_once(db_session, project.id)
        assert third == 1
    finally:
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = (
            original
        )


@pytest.mark.asyncio
async def test_fix_loop_divergence_fingerprint_feature_flag_gates_spawn(db_session):
    import app.shared_state as _shared_state

    original = _shared_state.runtime_config.get(
        "fix_loop_divergence_fingerprint_enabled", False
    )
    _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = False
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    for tail in (
        "TypeError: build failed for job 123 size 10MB",
        "TypeError: build failed for job 456 size 11MB",
        "TypeError: build failed for job 789 size 12MB",
    ):
        db_session.add(
            _make_fix_loop_divergence_task(
                project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]
            )
        )
    await db_session.commit()
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 0
    finally:
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = (
            original
        )


@pytest.mark.asyncio
async def test_end_to_end_chain_to_remediation(db_session, mocker):
    """Spawned investigate → diagnosis JSON → _handle_investigation_completion → remediation create (case e)."""
    from app.task_completion import _handle_investigation_completion

    # Patch the broadcast_to_project import _handle_investigation_completion
    # uses; tests don't need the websocket fan-out.
    mocker.patch(
        "app.task_completion.broadcast_to_project",
        new=mocker.AsyncMock(),
    )

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    investigation = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string() == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert investigation is not None

    diagnosis_report = {
        "summary": "self-review prompt drifted; correctness category drops on regex edge cases",
        "root_cause": "self-review prompt no longer enumerates correctness sub-checks for regex edges",
        "recommended_fix": "restore the correctness checklist block in the self-review prompt template",
        "confidence": "high",
        "severity": "high",
        "affected_files": [
            {"path": "agent/src/prompts/self-review.ts", "issue": "missing checklist"}
        ],
    }
    payload = SimpleNamespace(logs=json.dumps(diagnosis_report))

    await _handle_investigation_completion(investigation, payload, db_session)
    await db_session.commit()

    remediation = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.parent_task_id == investigation.id,
                )
            )
        )
        .scalars()
        .first()
    )
    assert remediation is not None
    rpayload = remediation.first_payload or {}
    assert rpayload.get("type") == "create"
    assert "restore the correctness checklist block" in rpayload.get("prompt", "")
    assert remediation.context.get("diagnosis_task_id") == str(investigation.id)
    # Synthetic direction key carries the regression class forward.
    assert remediation.context.get("direction_id", "").startswith(
        "pipeline_regression:"
    )


# ---------------------------------------------------------------------------
# RegressionRemediationCase creation / update / suppression tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sr_spawn_creates_remediation_case(db_session):
    """First spawn for an SR regression creates a RegressionRemediationCase row."""
    from app.models import RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.hit_count == 1
    snap = case.signal_snapshot_at_spawn or {}
    assert snap["prior_rate"] == 0.80
    assert snap["current_rate"] == 0.50
    assert snap["signal_name"] == "sr_catch_rate"
    assert "captured_at" in snap


@pytest.mark.asyncio
async def test_sr_repeat_spawn_updates_existing_case(db_session):
    """A second spawn beyond the cooldown increments hit_count on the existing case."""
    from app.models import RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    # Expire cooldown: mark the spawned task COMPLETED and backdate it.
    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    task.status = TaskStatus.COMPLETED
    task.created_at = _utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5)
    db_session.add(task)
    await db_session.commit()

    spawned2 = await run_once(db_session, project.id)
    assert spawned2 == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.hit_count == 2


@pytest.mark.asyncio
async def test_spawn_sets_investigate_spawned_at(db_session):
    """First spawn populates investigate_spawned_at on the new RegressionRemediationCase."""
    from app.models import RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.investigate_spawned_at is not None
    assert case.time_to_investigate_sec is not None
    assert case.time_to_investigate_sec >= 0


@pytest.mark.asyncio
async def test_repeat_spawn_preserves_investigate_spawned_at(db_session):
    """A second spawn does NOT overwrite the original investigate_spawned_at."""
    from app.models import RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    original_investigate_spawned_at = case.investigate_spawned_at
    assert original_investigate_spawned_at is not None

    # Expire cooldown and re-spawn.
    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    task.status = TaskStatus.COMPLETED
    task.created_at = _utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5)
    db_session.add(task)
    await db_session.commit()

    spawned2 = await run_once(db_session, project.id)
    assert spawned2 == 1

    await db_session.refresh(case)
    assert case.investigate_spawned_at == original_investigate_spawned_at


@pytest.mark.asyncio
async def test_manual_intervention_case_suppresses_spawn(db_session):
    """Existing case in NEEDS_MANUAL_INTERVENTION status prevents a new spawn."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
            hit_count=3,
        )
    )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert tasks == []


# ---------------------------------------------------------------------------
# GET /api/admin/pipeline-regressions — endpoint contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_regressions_endpoint_healthy(client, db_session):
    """No active signals → empty regressions list, healthy=true."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    response = await client.get(
        f"/api/admin/pipeline-regressions?project_id={project.id}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is True
    assert body["regressions"] == []


@pytest.mark.asyncio
async def test_pipeline_regressions_endpoint_emits_ui_contract(client, db_session):
    """Active SR + fix-loop signals are surfaced with the field names the UI consumes,
    and a dispatcher-spawned task using the colon-form regression_class enriches the row.
    """
    from datetime import datetime as _datetime

    now = _datetime.utcnow()
    iso_year, iso_week, _ = now.isocalendar()
    project = make_project(
        design_doc={
            "sr_catch_rate_trends_alert": {
                "categories": ["correctness"],
                "computed_at": now.isoformat(),
            },
            "sr_catch_rate_trends": {
                "categories": {
                    "correctness": {
                        "current_hit_rate": 0.50,
                        "previous_hit_rate": 0.80,
                    },
                },
            },
            "fix_loop_divergence_alerted_window": f"{iso_year}-W{iso_week:02d}",
            "fix_loop_divergence_detected_at": now.isoformat(),
            "fix_loop_divergence_regressions": [
                {"loop": "lint", "current_rate": 0.45, "prior_rate": 0.20},
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed one in-flight dispatcher task using the COLON form the dispatcher
    # actually persists (sr_catch_rate:correctness). The endpoint must use
    # the same key so by_class.get(regression_class) hits.
    inflight = make_task(
        project_id=project.id,
        title="Investigate SR regression: correctness",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate"}],
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate:correctness",
        },
    )
    db_session.add(inflight)
    await db_session.commit()
    await db_session.refresh(inflight)

    response = await client.get(
        f"/api/admin/pipeline-regressions?project_id={project.id}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is False

    regressions = body["regressions"]
    assert len(regressions) == 2

    # Locate the SR row and confirm the dispatcher task enriched it.
    sr_row = next(
        r for r in regressions if r["regression_class"] == "sr_catch_rate:correctness"
    )
    # Field names match admin-ui PipelineRegression interface.
    assert "signal_name" in sr_row
    assert sr_row["severity"] == "warning"  # lowercase, not WARNING
    assert sr_row["resolved"] is False
    assert sr_row["spawned_task_id"] == str(inflight.id)
    assert sr_row["spawned_task_status"] == "IN_PROGRESS"
    assert sr_row["spawned_task_title"] == "Investigate SR regression: correctness"
    assert sr_row["current_rate"] == 0.50
    assert sr_row["prior_rate"] == 0.80

    # Fix-loop row uses colon form and has no in-flight task → null spawned fields.
    fl_row = next(
        r for r in regressions if r["regression_class"] == "fix_loop_divergence:lint"
    )
    assert fl_row["severity"] == "warning"
    assert fl_row["spawned_task_id"] is None
    assert fl_row["spawned_task_status"] is None


@pytest.mark.asyncio
async def test_pipeline_regressions_endpoint_404_unknown_project(client):
    """Unknown project_id → 404."""
    bogus = uuid.uuid4()
    response = await client.get(f"/api/admin/pipeline-regressions?project_id={bogus}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Anomaly signal evaluator tests
# ---------------------------------------------------------------------------


def _make_anomaly_entry(
    *,
    occurrence_count: int = 5,
    last_seen: str | None = None,
    first_seen: str | None = None,
    example_message: str = "GCP quota exhausted",
    severity: str = "high",
) -> dict:
    from app.time_utils import _utcnow

    now = _utcnow()
    return {
        "occurrence_count": occurrence_count,
        "last_seen": last_seen or now.isoformat(),
        "first_seen": first_seen
        or (now.replace(hour=0, minute=0, second=0)).isoformat(),
        "example_message": example_message,
        "severity": severity,
    }


@pytest.mark.asyncio
async def test_anomaly_dispatch_on_threshold(db_session):
    """Anomaly meeting occurrence + recency thresholds spawns an investigation."""
    import app.shared_state as _ss

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "gcp_quota_exhaustion"
    _ss.persistent_anomalies[str(project.id)] = {
        fingerprint: _make_anomaly_entry(occurrence_count=5),
    }
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1

        task = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .first()
        )
        assert task is not None
        payload = task.first_payload or {}
        assert payload.get("type") == "investigate"
        ctx = task.context or {}
        assert ctx["regression_class"] == f"anomaly:{fingerprint}"
        assert ctx["signal_name"] == "persistent_anomaly"
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_anomaly_below_threshold_no_spawn(db_session):
    """Anomaly below occurrence threshold does not spawn."""
    import app.shared_state as _ss

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _ss.persistent_anomalies[str(project.id)] = {
        "low_count_anomaly": _make_anomaly_entry(occurrence_count=2),
    }
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 0
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_anomaly_stale_last_seen_no_spawn(db_session):
    """Anomaly outside the recency window does not spawn."""
    import app.shared_state as _ss
    from app.time_utils import _utcnow
    from datetime import timedelta

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_ts = (_utcnow() - timedelta(hours=72)).isoformat()
    _ss.persistent_anomalies[str(project.id)] = {
        "old_anomaly": _make_anomaly_entry(occurrence_count=10, last_seen=old_ts),
    }
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 0
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_anomaly_signal_snapshot_captured(db_session):
    """signal_snapshot_at_spawn records the expected anomaly fields."""
    import app.shared_state as _ss
    from app.models import RegressionRemediationCase
    from app.time_utils import _utcnow

    now = _utcnow()
    first_seen_iso = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    last_seen_iso = now.isoformat()
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "vm_provision_failure"
    _ss.persistent_anomalies[str(project.id)] = {
        fingerprint: {
            "occurrence_count": 7,
            "last_seen": last_seen_iso,
            "first_seen": first_seen_iso,
            "example_message": "VM provisioning timed out",
            "severity": "critical",
        },
    }
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1

        case = (
            (
                await db_session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project.id,
                        RegressionRemediationCase.regression_class
                        == f"anomaly:{fingerprint}",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert case is not None
        snap = case.signal_snapshot_at_spawn or {}
        assert snap["occurrence_count"] == 7
        assert snap["hit_count"] == 7
        assert snap["first_seen"] == first_seen_iso
        assert "last_seen" in snap
        assert snap["sample_message"] == "VM provisioning timed out"
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_anomaly_cooldown_blocks_re_trigger(db_session):
    """After an anomaly spawns, cooldown window prevents immediate re-spawn."""
    import app.shared_state as _ss

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "repeated_timeout"
    _ss.persistent_anomalies[str(project.id)] = {
        fingerprint: _make_anomaly_entry(occurrence_count=4),
    }
    try:
        first = await run_once(db_session, project.id)
        assert first == 1

        second = await run_once(db_session, project.id)
        assert second == 0

        tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 1
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_anomaly_dispatch_hydrates_from_design_doc(db_session):
    """After simulated pod restart (persistent_anomalies cleared), dispatch reads
    anomaly state from project.design_doc and spawns when threshold is met."""
    import app.shared_state as _ss
    from app.shared_state import _ANOMALY_FINGERPRINTS_KEY

    fingerprint = "provision_timeout"
    entry = _make_anomaly_entry(occurrence_count=5)
    project = make_project(design_doc={_ANOMALY_FINGERPRINTS_KEY: {fingerprint: entry}})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _ss.persistent_anomalies.pop(str(project.id), None)
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1, "Dispatcher must hydrate from design_doc and spawn"

        task = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .first()
        )
        assert task is not None
        assert (task.context or {}).get("regression_class") == f"anomaly:{fingerprint}"
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_recovery_scan_anomaly_closed_via_durable_state(db_session):
    """Recovery scan closes a MERGED anomaly case using project.design_doc after
    simulated pod restart (persistent_anomalies cleared). Anomaly count below threshold
    signals recovery."""
    import app.shared_state as _ss
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.shared_state import _ANOMALY_FINGERPRINTS_KEY

    fingerprint = "gcp_quota_exhaustion"
    # Entry with count below threshold — anomaly has recovered.
    recovered_entry = _make_anomaly_entry(occurrence_count=1)
    project = make_project(
        design_doc={_ANOMALY_FINGERPRINTS_KEY: {fingerprint: recovered_entry}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(minutes=20)).isoformat()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=f"anomaly:{fingerprint}",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    _ss.persistent_anomalies.pop(str(project.id), None)
    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


@pytest.mark.asyncio
async def test_recovery_scan_anomaly_rearmed_via_durable_state(db_session):
    """Recovery scan re-arms a MERGED anomaly case when durable state shows the
    anomaly still exceeds the threshold (pod-restart simulation)."""
    import app.shared_state as _ss
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.shared_state import _ANOMALY_FINGERPRINTS_KEY

    fingerprint = "vm_provision_failure"
    active_entry = _make_anomaly_entry(occurrence_count=10)
    project = make_project(
        design_doc={_ANOMALY_FINGERPRINTS_KEY: {fingerprint: active_entry}}
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(minutes=20)).isoformat()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=f"anomaly:{fingerprint}",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    _ss.persistent_anomalies.pop(str(project.id), None)
    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE


@pytest.mark.asyncio
async def test_pipeline_regressions_endpoint_enriches_crash_investigation(
    client, db_session
):
    """A background-service crash alert is enriched with the lifecycle-spawned
    crash_investigation task — even though crash investigations use
    spawned_by='crash_investigation' (not 'pipeline_regression_dispatcher').
    """
    from app.models import Alert, AlertSeverity

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    service_name = "reaper"
    db_session.add(
        Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title=f"Background service crashed: {service_name}",
            message="Service crashed with: RuntimeError",
        )
    )
    # lifecycle._spawn_crash_investigations writes spawned_by='crash_investigation'
    # plus regression_class='background_service_crash:<service>'.
    inflight = make_task(
        project_id=project.id,
        title=f"Investigate repeated crash: {service_name} service",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate"}],
        context={
            "spawned_by": "crash_investigation",
            "service_name": service_name,
            "regression_class": f"background_service_crash:{service_name}",
        },
    )
    db_session.add(inflight)
    await db_session.commit()
    await db_session.refresh(inflight)

    response = await client.get(
        f"/api/admin/pipeline-regressions?project_id={project.id}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is False

    crash_rows = [
        r
        for r in body["regressions"]
        if r["regression_class"] == f"background_service_crash:{service_name}"
    ]
    assert len(crash_rows) == 1
    crash = crash_rows[0]
    assert crash["severity"] == "critical"
    assert crash["spawned_task_id"] == str(inflight.id)
    assert crash["spawned_task_status"] == "IN_PROGRESS"
    assert (
        crash["spawned_task_title"]
        == f"Investigate repeated crash: {service_name} service"
    )


# ---------------------------------------------------------------------------
# Recovery scan — run_recovery_once tests
# ---------------------------------------------------------------------------


def _merged_case(
    project_id,
    *,
    regression_class: str = "sr_catch_rate:correctness",
    attempts: int = 0,
    minutes_since_merge: int = 20,
) -> RegressionRemediationCase:  # noqa: F821
    """Build a MERGED RegressionRemediationCase whose cooldown has elapsed."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    merged_at = (_utcnow() - timedelta(minutes=minutes_since_merge)).isoformat()
    return RegressionRemediationCase(
        project_id=project_id,
        regression_class=regression_class,
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=attempts,
        signal_snapshot_at_merge={"captured_at": merged_at},
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
        fingerprint_to_fix_log=[],
    )


@pytest.mark.asyncio
async def test_recovery_scan_closes_recovered_signal(db_session):
    """Signal no longer tripped after merge → mark_closed(), status=CLOSED, log appended."""
    from app.models import RegressionCaseStatus

    # Design doc shows recovery: prior ≈ current (no regression).
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.80,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _merged_case(project.id)
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    # Recovery log entry appended.
    log = case.fingerprint_to_fix_log or []
    assert any("recovered_at" in e for e in log)


@pytest.mark.asyncio
async def test_recovery_scan_rearms_on_still_tripped(db_session):
    """Signal still tripped after merge → re_arm(), status=ACTIVE, attempts incremented."""
    from app.models import RegressionCaseStatus

    # SR still regressed: 30pp drop.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _merged_case(project.id, attempts=0)
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.attempts == 1


@pytest.mark.asyncio
async def test_recovery_scan_third_rearm_needs_manual(db_session):
    """Third re-arm (attempts already at 2) escalates to NEEDS_MANUAL_INTERVENTION."""
    from app.models import RegressionCaseStatus

    # Still tripped.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _merged_case(project.id, attempts=2)
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None


@pytest.mark.asyncio
async def test_recovery_rearm_deferred_when_spawn_cooldown_active(db_session):
    """re-arm is deferred when last dispatcher spawn is within 6h cooldown (case a).

    Status stays MERGED; a subsequent run_once tick must not spawn either.
    """
    from app.models import RegressionCaseStatus

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Use the aggregate "sr_catch_rate" class — what run_once actually spawns.
    case = _merged_case(project.id, attempts=0, regression_class="sr_catch_rate")
    db_session.add(case)

    # Prior dispatcher-spawned task is 1h old — within the 6h cooldown window.
    prior_spawn = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=1),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(prior_spawn)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 0  # deferred — nothing committed

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.MERGED  # still MERGED, not re-armed

    # The dispatcher tick must also skip (MERGED cases are owned by recovery scan).
    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_recovery_rearm_proceeds_after_cooldown_elapsed(db_session):
    """re-arm fires normally once the 6h spawn cooldown has elapsed (case b)."""
    from app.models import RegressionCaseStatus

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _merged_case(project.id, attempts=0, regression_class="sr_catch_rate")
    db_session.add(case)

    # Prior spawn is older than 6h — cooldown window has elapsed.
    prior_spawn = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(prior_spawn)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.attempts == 1


@pytest.mark.asyncio
async def test_recovery_close_unaffected_by_spawn_cooldown(db_session):
    """CLOSED transition fires regardless of active spawn cooldown (case c)."""
    from app.models import RegressionCaseStatus

    # Signal recovered: equal hit rates → not tripped.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.80,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _merged_case(project.id, attempts=0, regression_class="sr_catch_rate")
    db_session.add(case)

    # Prior spawn is 1h old — still within 6h cooldown.
    prior_spawn = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=1),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(prior_spawn)
    await db_session.commit()
    await db_session.refresh(case)

    # Signal not tripped → CLOSED path must not be blocked by spawn cooldown.
    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


@pytest.mark.asyncio
async def test_recovery_cooldown_unchanged_by_spawn_cooldown_fix(db_session):
    """15-min post-merge recovery wait is still enforced (case d)."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Merge only 5 min ago — within the 15-min recovery cooldown.
    case = _merged_case(project.id, minutes_since_merge=5)
    db_session.add(case)
    await db_session.commit()

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 0  # skipped because recovery cooldown not yet elapsed


@pytest.mark.asyncio
async def test_recovery_scan_no_double_close(db_session):
    """Already-CLOSED case is not picked up by the recovery scan."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.80,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(minutes=20)).isoformat()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.CLOSED,
        hit_count=1,
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 0


@pytest.mark.asyncio
async def test_recovery_scan_respects_cooldown(db_session):
    """MERGED case whose signal_snapshot_at_merge was captured within the 15-min
    cooldown is skipped — signals need time to settle post-merge."""

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.80,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Merge timestamp < 15 min ago — within the cooldown window.
    case = _merged_case(project.id, minutes_since_merge=5)
    db_session.add(case)
    await db_session.commit()

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 0


@pytest.mark.asyncio
async def test_investigate_fix_merge_recovery_closed(db_session, mocker):
    """Full investigate→fix→merge→recovery flow: MERGED → CLOSED when signal recovered.

    Regression guard: signal_snapshot propagated through remediation_context so
    mark_merged receives a non-empty snapshot and run_recovery_once can evaluate.
    """
    from types import SimpleNamespace
    import json as _json
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.task_completion import _handle_investigation_completion
    from app.task_lifecycle import _close_loop_regression_case_on_merge

    mocker.patch("app.task_completion.broadcast_to_project", new=mocker.AsyncMock())

    # Initial state: regression is present so run_once spawns an investigation.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spawn the initial investigation (creates the RegressionRemediationCase).
    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    investigation = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string() == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert investigation is not None

    # Complete the investigation → spawns a remediation 'create' task.
    diagnosis = {
        "summary": "sr correctness drop",
        "root_cause": "prompt drift",
        "recommended_fix": "restore checklist",
        "confidence": "high",
        "severity": "high",
        "affected_files": [
            {"path": "agent/src/prompts/self-review.ts", "issue": "drift"}
        ],
    }
    await _handle_investigation_completion(
        investigation,
        SimpleNamespace(logs=_json.dumps(diagnosis)),
        db_session,
    )
    await db_session.commit()

    remediation = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.parent_task_id == investigation.id,
                )
            )
        )
        .scalars()
        .first()
    )
    assert remediation is not None
    # regression_class must be propagated so mark_merged can stamp the snapshot.
    assert remediation.context.get("regression_class") == "sr_catch_rate"
    assert remediation.context.get("signal_snapshot"), (
        "signal_snapshot must be propagated into remediation context"
    )

    # Simulate the PR merging: stamp MERGED with the signal snapshot.
    await _close_loop_regression_case_on_merge(remediation, 9999, db_session)
    await db_session.commit()

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.status == RegressionCaseStatus.MERGED
    assert case.signal_snapshot_at_merge, (
        "mark_merged must receive a non-empty snapshot"
    )

    # Backdate the merge so recovery cooldown has elapsed.
    snap = dict(case.signal_snapshot_at_merge)
    snap["captured_at"] = (_utcnow() - timedelta(minutes=20)).isoformat()
    case.signal_snapshot_at_merge = snap
    db_session.add(case)

    # Simulate the signal recovering: update design_doc so run_recovery_once sees no regression.
    project.design_doc = _design_doc_with_sr_regression(
        category="correctness",
        prior=0.80,
        current=0.80,
    )
    db_session.add(project)
    await db_session.commit()

    # Recovery scan sees a recovered signal → CLOSED.
    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


@pytest.mark.asyncio
async def test_mark_merged_fallback_when_signal_snapshot_missing(db_session, mocker):
    """Regression guard: when remediation task context lacks signal_snapshot,
    mark_merged falls back to signal_snapshot_at_spawn and run_recovery_once
    can still close the case.

    Exercises the bug path: old code left signal_snapshot_at_merge empty for
    tasks that went through investigate→fix without context propagation.
    """
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.task_lifecycle import _close_loop_regression_case_on_merge

    mocker.patch("app.task_completion.broadcast_to_project", new=mocker.AsyncMock())

    # Initial state: regression is present so run_once spawns and creates the case.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spawn creates the RegressionRemediationCase with signal_snapshot_at_spawn.
    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    # Build a minimal remediation task carrying regression_class but missing
    # signal_snapshot — simulating the pre-fix bug state.
    remediation = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "fix it", "base_branch": "main"}],
        context={"regression_class": "sr_catch_rate"},
    )
    db_session.add(remediation)
    await db_session.commit()
    await db_session.refresh(remediation)

    await _close_loop_regression_case_on_merge(remediation, 1234, db_session)
    await db_session.commit()

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.status == RegressionCaseStatus.MERGED
    # Fallback must populate signal_snapshot_at_merge from signal_snapshot_at_spawn.
    assert case.signal_snapshot_at_merge, (
        "mark_merged fallback must produce a non-empty snapshot from signal_snapshot_at_spawn"
    )

    # Backdate merge so recovery cooldown has elapsed.
    snap = dict(case.signal_snapshot_at_merge)
    snap["captured_at"] = (_utcnow() - timedelta(minutes=20)).isoformat()
    case.signal_snapshot_at_merge = snap
    db_session.add(case)

    # Simulate the signal recovering before the recovery scan.
    project.design_doc = _design_doc_with_sr_regression(
        category="correctness",
        prior=0.80,
        current=0.80,
    )
    db_session.add(project)
    await db_session.commit()

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


# ---------------------------------------------------------------------------
# Watchdog — run_watchdog_once tests
# ---------------------------------------------------------------------------


async def _make_stale_case(
    db_session,
    project_id,
    *,
    status: RegressionCaseStatus,  # noqa: F821
    hours_stale: int,
    regression_class: str = "sr_catch_rate:correctness",
) -> RegressionRemediationCase:  # noqa: F821
    """Create a case and backdate its updated_at to simulate staleness."""
    from sqlalchemy import update
    from app.models import RegressionRemediationCase as RRC

    case = RRC(
        project_id=project_id,
        regression_class=regression_class,
        status=status,
        hit_count=1,
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    stale_ts = _utcnow() - timedelta(hours=hours_stale)
    await db_session.execute(
        update(RRC).where(RRC.id == case.id).values(updated_at=stale_ts)
    )
    await db_session.commit()
    await db_session.refresh(case)
    return case


@pytest.mark.asyncio
async def test_watchdog_active_stale_triggers_alert(db_session):
    """ACTIVE case older than 24h threshold escalates to NEEDS_MANUAL_INTERVENTION and opens Alert (case a)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_hours = int(_WATCHDOG_ACTIVE_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.ACTIVE,
        hours_stale=stale_hours,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert any("sr_catch_rate:correctness" in (a.title or "") for a in alerts)


@pytest.mark.asyncio
async def test_watchdog_merged_stale_triggers_alert(db_session):
    """MERGED case older than 6h threshold escalates to NEEDS_MANUAL_INTERVENTION and opens Alert (case b)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_hours = int(_WATCHDOG_MERGED_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.MERGED,
        hours_stale=stale_hours,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert any("sr_catch_rate:correctness" in (a.title or "") for a in alerts)


@pytest.mark.asyncio
async def test_watchdog_recent_case_not_triggered(db_session):
    """Case with recent updated_at does not trigger the watchdog (case c)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Only 1 hour old — well within both thresholds.
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.ACTIVE,
        hours_stale=1,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_does_not_re_escalate_needs_manual_intervention(db_session):
    """NEEDS_MANUAL_INTERVENTION cases are excluded from re-escalation by the watchdog.

    Previously the watchdog skipped these cases entirely; now it examines them
    for auto-close, but only escalation (the WARNING alert path) is still
    skipped. With an empty design_doc the signal value cannot be evaluated this
    tick, so within a single watchdog call the case stays NMI and no alert is
    emitted — matching the prior contract for this case.
    """
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Already escalated — very stale but the watchdog must not re-escalate.
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=48,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0  # No escalation, no auto-close (window not elapsed)

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_closed_case_never_escalated(db_session):
    """CLOSED cases are never escalated by the watchdog (case e)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Very stale CLOSED case — should still be skipped by watchdog.
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.CLOSED,
        hours_stale=48,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_merged_escalation_idempotent(db_session):
    """Running watchdog twice does not double-escalate or duplicate alerts (case f)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_hours = int(_WATCHDOG_MERGED_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.MERGED,
        hours_stale=stale_hours,
    )

    # First watchdog run — escalate the stale MERGED case.
    escalated_1 = await run_watchdog_once(db_session, project.id)
    assert escalated_1 == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None

    alerts_1 = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts_1) == 1

    # Second watchdog run — case is already NEEDS_MANUAL_INTERVENTION, should not re-escalate.
    escalated_2 = await run_watchdog_once(db_session, project.id)
    assert escalated_2 == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION

    alerts_2 = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts_2) == 1  # No new alert created


# ---------------------------------------------------------------------------
# Watchdog auto-escalation (one-shot meta-investigate) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_active_stale_with_open_child_auto_escalates(db_session):
    """ACTIVE-stale case with an in-flight child spawns a meta-investigate before NMI.

    The original investigation is still running, so the watchdog gives the
    autonomous loop ONE more attempt at the remediation blocker before paging
    the operator. Status stays ACTIVE; auto_escalation_count goes 0 → 1; the
    new task is tagged with `auto_escalation=True` and an INFO Alert is opened.
    """
    from app.models import Alert, AlertSeverity, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # In-flight child task that the case points at — keeps `_has_active_remediation_child` True.
    child = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"spawned_by": "pipeline_regression_dispatcher"},
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    stale_hours = int(_WATCHDOG_ACTIVE_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.ACTIVE,
        hours_stale=stale_hours,
    )
    # Wire the child onto the case directly via SQL so the test does not depend
    # on _make_stale_case accepting the link as an argument. Re-stamp updated_at
    # in the same UPDATE so the onupdate hook does not refresh it back to "now".
    from sqlalchemy import update as sql_update
    from app.models import RegressionRemediationCase as RRC

    stale_ts = _utcnow() - timedelta(hours=stale_hours)
    await db_session.execute(
        sql_update(RRC)
        .where(RRC.id == case.id)
        .values(last_remediation_task_id=child.id, updated_at=stale_ts)
    )
    await db_session.commit()
    await db_session.refresh(case)

    mutated = await run_watchdog_once(db_session, project.id)
    assert mutated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.auto_escalation_count == 1
    assert case.last_remediation_task_id != child.id  # now points at meta task

    # The new meta-investigate task exists with the expected tagging.
    meta_tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.id != child.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(meta_tasks) == 1
    meta = meta_tasks[0]
    assert (meta.context or {}).get("auto_escalation") is True
    assert (meta.context or {}).get("signal_name") == "auto_escalation_meta_investigate"
    assert (meta.context or {}).get("regression_class") == case.regression_class
    assert (meta.first_payload or {}).get("type") == "investigate"

    # Alert is INFO, not WARNING — the operator is informed but not paged.
    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.INFO


@pytest.mark.asyncio
async def test_watchdog_active_stale_already_auto_escalated_promotes_to_nmi(db_session):
    """A second 24h stall after auto-escalation transitions to NEEDS_MANUAL_INTERVENTION.

    `auto_escalation_count` is the cooldown gate — capped at 1. Once the
    auto-escalation has fired and another 24h has elapsed without resolution,
    the watchdog falls through to the legacy NMI + WARNING Alert path.
    """
    from app.models import Alert, AlertSeverity, RegressionCaseStatus
    from sqlalchemy import update as sql_update
    from app.models import RegressionRemediationCase as RRC

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Even with an in-flight child, count >= 1 forces NMI.
    child = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"spawned_by": "pipeline_regression_dispatcher_watchdog"},
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    stale_hours = int(_WATCHDOG_ACTIVE_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.ACTIVE,
        hours_stale=stale_hours,
    )
    stale_ts = _utcnow() - timedelta(hours=stale_hours)
    await db_session.execute(
        sql_update(RRC)
        .where(RRC.id == case.id)
        .values(
            last_remediation_task_id=child.id,
            auto_escalation_count=1,
            updated_at=stale_ts,
        )
    )
    await db_session.commit()
    await db_session.refresh(case)

    mutated = await run_watchdog_once(db_session, project.id)
    assert mutated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None
    assert case.auto_escalation_count == 1  # not incremented again

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING


@pytest.mark.asyncio
async def test_watchdog_active_stale_no_open_child_skips_auto_escalation(db_session):
    """ACTIVE-stale case with NO open child goes straight to NMI (different bug class).

    When `last_remediation_task_id` is None or the child has terminated, the
    investigation finished but no fix landed — that's the original bug class
    the watchdog has always paged on. Auto-escalation only applies when the
    child is genuinely still working.
    """
    from app.models import Alert, AlertSeverity, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_hours = int(_WATCHDOG_ACTIVE_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.ACTIVE,
        hours_stale=stale_hours,
    )
    # No last_remediation_task_id — the case has no in-flight child.

    mutated = await run_watchdog_once(db_session, project.id)
    assert mutated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.auto_escalation_count == 0  # untouched

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING


# ---------------------------------------------------------------------------
# Watchdog auto-close (recovered_unattended) tests
# ---------------------------------------------------------------------------


def _design_doc_no_sr_regression(*, category: str = "correctness") -> dict:
    """Design-doc snapshot where the SR catch-rate signal would NOT trip.

    Mirror of ``_design_doc_with_sr_regression`` but with prior == current so
    the trend delta is zero and ``_current_signal_value_for_class`` reports
    ``tripped=False``.
    """
    return {
        "sr_catch_rate_trends": {
            "categories": {
                category: {
                    "current_hit_rate": 0.80,
                    "previous_hit_rate": 0.80,
                    "current_human_count": 12,
                    "trend": "stable",
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_watchdog_no_auto_close_when_signal_still_tripping(db_session):
    """NMI case is left untouched when the signal is still tripping (1)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=48,
        regression_class="sr_catch_rate:correctness",
    )

    # Pre-populate healthy_since at 30h ago so a falsely-healthy read would
    # auto-close. The signal IS still tripping per the design-doc above, so
    # the dispatcher must drop the marker and skip.
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.last_remediation_result != "recovered_unattended"
    # Marker dropped on the case row because signal would trip today.
    assert case.signal_healthy_since_at is None

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_no_auto_close_within_window(db_session):
    """NMI case stays open when healthy_since is younger than the window (2)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=48,
        regression_class="sr_catch_rate:correctness",
    )

    seeded_healthy_at = _utcnow() - timedelta(hours=12)
    case.signal_healthy_since_at = seeded_healthy_at
    db_session.add(case)
    await db_session.commit()

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.last_remediation_result != "recovered_unattended"
    # Marker preserved on the case row — signal is healthy, just not long
    # enough yet — so the timer must persist verbatim across ticks (and
    # across worker restarts in production).
    assert case.signal_healthy_since_at is not None

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_auto_closes_after_window(db_session):
    """NMI case auto-closes once healthy_since exceeds the 24h window (3)."""
    from app.models import Alert, AlertSeverity, RegressionCaseStatus

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )

    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "recovered_unattended"

    log_entries = case.fingerprint_to_fix_log or []
    assert any(
        isinstance(e, dict) and e.get("reason") == "recovered_unattended"
        for e in log_entries
    )

    # Recovery clock cleared on close so the column never carries a stale
    # value into a future re-escalation.
    assert case.signal_healthy_since_at is None

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    info_alerts = [a for a in alerts if a.severity == AlertSeverity.INFO]
    assert len(info_alerts) == 1
    assert "auto-recovered" in (info_alerts[0].title or "").lower()


@pytest.mark.asyncio
async def test_watchdog_auto_close_race_with_operator_clear(db_session):
    """Auto-close skips a case whose status changed between scan and lock (4).

    Simulates the operator pressing the admin Clear button (which transitions
    the case back to ACTIVE) just before the watchdog locks the row. The
    re-check on the locked SELECT (``status == NEEDS_MANUAL_INTERVENTION``)
    causes the watchdog to skip — no double-write, no alert, no log entry.
    """
    from app.models import Alert, RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)

    # Operator clears the case before the watchdog runs — the lock SELECT's
    # status filter must reject the row even though the candidate scan would
    # have included it.
    case.status = RegressionCaseStatus.ACTIVE
    case.needs_manual_intervention_at = None
    db_session.add(case)
    await db_session.commit()

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 0

    refreshed = (
        await db_session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.id == case.id
            )
        )
    ).scalar_one()
    assert refreshed.status == RegressionCaseStatus.ACTIVE
    assert refreshed.last_remediation_result != "recovered_unattended"
    log_entries = refreshed.fingerprint_to_fix_log or []
    assert not any(
        isinstance(e, dict) and e.get("reason") == "recovered_unattended"
        for e in log_entries
    )

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_auto_close_window_runtime_config_override(db_session):
    """runtime_config['regression_recovered_unattended_window_hours'] takes effect (5)."""
    from app.models import RegressionCaseStatus
    from app.shared_state import runtime_config

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    # 90 minutes — under the 24h default but over the override.
    case.signal_healthy_since_at = _utcnow() - timedelta(minutes=90)
    db_session.add(case)
    await db_session.commit()

    original = runtime_config.get("regression_recovered_unattended_window_hours")
    runtime_config["regression_recovered_unattended_window_hours"] = 1
    try:
        closed = await run_watchdog_once(db_session, project.id)
    finally:
        if original is None:
            runtime_config.pop("regression_recovered_unattended_window_hours", None)
        else:
            runtime_config["regression_recovered_unattended_window_hours"] = original

    assert closed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "recovered_unattended"


@pytest.mark.asyncio
async def test_operator_clear_drops_healthy_since_marker(db_session):
    """Operator-clear path drops the process-local healthy_since marker.

    Lifecycle: NMI healthy 30h → operator clears (NMI→ACTIVE) → case
    re-escalates to NMI → first watchdog tick must NOT auto-close because
    the stable window timer is reset by the clear, not preserved across
    lifecycles.
    """
    from app.models import Alert, RegressionCaseStatus, RegressionRemediationCase
    from app.routers.admin_digest import clear_regression_manual_intervention

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    regression_class = "sr_catch_rate:correctness"
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class=regression_class,
    )
    # Stale healthy_since from a prior NMI lifecycle — would cause an
    # immediate auto-close on a re-escalation if not cleared by the operator
    # clear path.
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    # Operator clears via the admin endpoint — this must NULL out
    # signal_healthy_since_at on the case row.
    await clear_regression_manual_intervention(
        regression_class=regression_class,
        project_id=project.id,
        session=db_session,
        current_user={},
    )
    await db_session.refresh(case)
    assert case.signal_healthy_since_at is None

    # Re-escalate the case back to NMI (e.g. another stale-active tick).
    case.status = RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    case.needs_manual_intervention_at = _utcnow()
    db_session.add(case)
    await db_session.commit()

    # Watchdog tick: signal is healthy now, but the marker was wiped, so
    # the timer resets to "just became healthy" — no auto-close on first tick.
    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 0

    refreshed = (
        await db_session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.id == case.id
            )
        )
    ).scalar_one()
    assert refreshed.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert refreshed.last_remediation_result != "recovered_unattended"

    # Auto-recovered alert must NOT have been emitted.
    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title.like("Regression auto-recovered:%"),
                )
            )
        )
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_no_auto_close_when_signal_unevaluable(db_session):
    """state=None (e.g. empty design_doc) is treated as 'unknown', not 'healthy'.

    The watchdog must not start the healthy_since timer for a case whose
    signal cannot be evaluated — otherwise missing telemetry would silently
    retire the escalation after the stable window.
    """
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    # Empty design_doc → _current_signal_value_for_class returns None for
    # sr_catch_rate:correctness (no prior/current rate data available).
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 0

    refreshed = (
        await db_session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.id == case.id
            )
        )
    ).scalar_one()
    # Critically: no marker should have been set, so a subsequent tick 30h
    # later would also not auto-close.
    assert refreshed.signal_healthy_since_at is None
    assert refreshed.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION


# ---------------------------------------------------------------------------
# Marginal-improvement auto-close tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_close_clean_improvement_unattended(db_session):
    """Large improvement on a higher-is-better signal (pre=0.40, post=0.80) →
    recovered_unattended + INFO alert.

    sr_catch_rate is higher-is-better — the fixture's design doc reports
    current_hit_rate=0.80, so the watchdog reads post_value=0.80. Pre-fix
    snapshot at 0.40 means a +0.40 absolute improvement, which clears both
    floors comfortably.
    """
    from app.models import Alert, AlertSeverity, RegressionCaseStatus

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    # Pre-fix snapshot: hit-rate was at 0.40 before the fix merged; design-doc
    # current_hit_rate=0.80 means post_value=0.80. Improvement = +0.40.
    case.signal_snapshot_at_merge = {"current_rate": 0.40}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "recovered_unattended"

    log_entries = case.fingerprint_to_fix_log or []
    assert any(
        isinstance(e, dict) and e.get("reason") == "recovered_unattended"
        for e in log_entries
    )

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.INFO


@pytest.mark.asyncio
async def test_auto_close_marginal_improvement_emits_warn_alert(db_session):
    """Tiny improvement on a higher-is-better signal (pre=0.78, post=0.80) →
    recovered_marginal + WARNING alert.

    sr_catch_rate is higher-is-better; design-doc current_hit_rate=0.80 so
    post_value=0.80. Pre-fix snapshot=0.78 → improvement=0.02 which is below
    the absolute floor (0.05) AND below the relative floor — relative is
    measured against the headroom (1.0 - pre = 0.22), giving 0.02/0.22 ≈ 9%
    < rel_floor(10%).
    """
    from app.models import Alert, AlertSeverity, RegressionCaseStatus

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    # Pre-fix=0.78, post-fix=0.80 (from design-doc current_hit_rate). Improvement
    # = 0.02 < abs_floor(0.05) AND relative = 0.02/0.22 ≈ 9% < rel_floor(10%).
    case.signal_snapshot_at_merge = {"current_rate": 0.78}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "recovered_marginal"

    log_entries = case.fingerprint_to_fix_log or []
    assert any(
        isinstance(e, dict) and e.get("reason") == "recovered_marginal"
        for e in log_entries
    )

    assert case.signal_healthy_since_at is None

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    warn_alert = alerts[0]
    assert warn_alert.severity == AlertSeverity.WARNING
    assert "marginal" in (warn_alert.title or "").lower()


@pytest.mark.asyncio
async def test_auto_close_missing_snapshot_falls_back_to_clean(db_session):
    """Missing signal_snapshot_at_merge → falls back to recovered_unattended + INFO."""
    from app.models import Alert, AlertSeverity, RegressionCaseStatus

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    # Snapshot is empty — no pre-fix value available.
    case.signal_snapshot_at_merge = {}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "recovered_unattended"

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.INFO


@pytest.mark.asyncio
async def test_auto_close_marginal_runtime_config_override(db_session):
    """Raising relative floor reclassifies a borderline-clean case as marginal."""
    from app.models import Alert, AlertSeverity, RegressionCaseStatus
    from app.shared_state import runtime_config

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    # sr_catch_rate is higher-is-better; design-doc current_hit_rate=0.80 so
    # post_value=0.80. Pre=0.50, improvement=0.30. Reference (headroom)=0.50,
    # relative=60%. Defaults (rel=10%, abs=5%) → clean. Override (rel=0.80,
    # abs=0.80) → 60% < 80% AND 0.30 < 0.80 → marginal.
    case.signal_snapshot_at_merge = {"current_rate": 0.50}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    original_rel = runtime_config.get("regression_recovered_marginal_relative_floor")
    original_abs = runtime_config.get("regression_recovered_marginal_absolute_floor")
    runtime_config["regression_recovered_marginal_relative_floor"] = 0.80
    runtime_config["regression_recovered_marginal_absolute_floor"] = 0.80
    try:
        closed = await run_watchdog_once(db_session, project.id)
    finally:
        for key, orig in [
            ("regression_recovered_marginal_relative_floor", original_rel),
            ("regression_recovered_marginal_absolute_floor", original_abs),
        ]:
            if orig is None:
                runtime_config.pop(key, None)
            else:
                runtime_config[key] = orig

    assert closed == 1
    await db_session.refresh(case)
    assert case.last_remediation_result == "recovered_marginal"

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING


@pytest.mark.asyncio
async def test_auto_close_higher_is_better_degradation_is_marginal(db_session):
    """A drop on a higher-is-better signal must NOT be flagged as a clean recovery.

    sr_catch_rate is higher-is-better. With pre=0.90 and post=0.80, the signal
    actually got worse — improvement is negative. This test would fail under
    the old hard-coded ``pre - post`` formula (which would treat 0.10 as a
    clean improvement) and protects against re-introducing that bug.
    """
    from app.models import Alert, AlertSeverity, RegressionCaseStatus

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    # post_value = 0.80 (design-doc), pre_value = 0.90 → degradation of 0.10.
    case.signal_snapshot_at_merge = {"current_rate": 0.90}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 1

    await db_session.refresh(case)
    assert case.last_remediation_result == "recovered_marginal"

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING


@pytest.mark.asyncio
async def test_recovery_cycle_record_tracks_marginal_and_confirmed_counts(db_session):
    """Recovery cycle records persist marginal and confirmed recovery counts."""
    from app.models import RegressionCaseStatus
    from app.pipeline_regression_dispatcher import get_dispatcher_cycle_records

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create two cases: one marginal, one confirmed
    marginal_case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    marginal_case.signal_snapshot_at_merge = {"current_rate": 0.78}
    marginal_case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(marginal_case)

    confirmed_case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="fix_loop_divergence",
    )
    confirmed_case.signal_snapshot_at_merge = {"current_rate": 100}
    confirmed_case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(confirmed_case)
    await db_session.commit()

    # Run watchdog to close both cases
    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 2

    # Verify cycle record has the correct counts
    cycles = await get_dispatcher_cycle_records(db_session, project_id=str(project.id))
    recovery_cycles = [
        c
        for c in cycles
        if c.get("marginal_recoveries_count", 0) > 0
        or c.get("confirmed_recoveries_count", 0) > 0
    ]
    assert len(recovery_cycles) >= 1
    latest = recovery_cycles[-1]
    assert latest["marginal_recoveries_count"] == 1
    assert latest["confirmed_recoveries_count"] == 1


# ---------------------------------------------------------------------------
# Deploy-monitoring gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_monitoring_defers_spawn(db_session):
    """Signal trip during fresh MONITORING window → no spawn (deploy noise gate)."""
    from app.models import DeploymentState, DeploymentStatus

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    # Deploy happened just now — well within the 30-min defer window.
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.MONITORING,
            deployed_at=_utcnow(),
        )
    )
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert tasks == []


@pytest.mark.asyncio
async def test_deploy_monitoring_defer_persists_alert(db_session):
    """Deferred spawn during MONITORING writes an operator-visible Alert."""
    from app.models import Alert, DeploymentState, DeploymentStatus

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.MONITORING,
            deployed_at=_utcnow(),
        )
    )
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert any("monitoring" in (a.title or "").lower() for a in alerts), (
        "deferred spawn must persist an Alert so operators know why the signal was not actioned"
    )


@pytest.mark.asyncio
async def test_deploy_monitoring_window_expired_allows_spawn(db_session):
    """After the 30-min defer window the dispatcher spawns even if still MONITORING."""
    from app.models import DeploymentState, DeploymentStatus
    from app.deploy_health import _DEPLOY_MONITORING_DEFER_WINDOW

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    # Deploy was 31 minutes ago — window has expired.
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.MONITORING,
            deployed_at=_utcnow()
            - _DEPLOY_MONITORING_DEFER_WINDOW
            - timedelta(minutes=1),
        )
    )
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1


@pytest.mark.asyncio
async def test_deploy_stable_after_monitoring_allows_spawn(db_session):
    """Once deploy_state flips to STABLE the dispatcher spawns normally."""
    from app.models import DeploymentState, DeploymentStatus

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.STABLE,
            deployed_at=_utcnow() - timedelta(hours=1),
        )
    )
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1


# ---------------------------------------------------------------------------
# Signal coalescing — bundle tests
# ---------------------------------------------------------------------------


def _design_doc_with_two_sr_regressions() -> dict:
    """Two categories both tripped — dispatcher must coalesce into one task."""
    return {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "current_hit_rate": 0.50,
                    "previous_hit_rate": 0.80,
                    "current_human_count": 12,
                    "trend": "decreasing",
                },
                "style-conventions": {
                    "current_hit_rate": 0.55,
                    "previous_hit_rate": 0.85,
                    "current_human_count": 15,
                    "trend": "decreasing",
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_two_sr_categories_coalesce_into_one_task(db_session):
    """Two simultaneous sr_catch_rate trips produce ONE investigate task with both signals."""
    from app.models import RegressionRemediationCase

    project = make_project(design_doc=_design_doc_with_two_sr_regressions())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1, "Two tripped SR categories must produce exactly one task"

    task = tasks[0]
    ctx = task.context or {}
    assert ctx["regression_class"] == "sr_catch_rate"
    signals = ctx.get("signals") or []
    assert "sr_catch_rate:correctness" in signals
    assert "sr_catch_rate:style-conventions" in signals

    payload = task.first_payload or {}
    assert payload.get("type") == "investigate"
    assert "bundle" in payload.get("prompt", "").lower() or len(signals) > 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None


# ---------------------------------------------------------------------------
# Context-aware second-attempt prompts
# ---------------------------------------------------------------------------


async def _seed_dispatcher_spawn_history(
    db_session,
    project_id,
    *,
    regression_class: str,
    created_at,
    pr_number: int,
) -> Task:
    task = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_at=created_at,
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": regression_class,
        },
        payloads=[{"type": "investigate", "prompt": "old", "base_branch": "main"}],
    )
    db_session.add(task)
    db_session.add(
        make_task_history(
            task.id,
            action="SPAWNED",
            timestamp=created_at,
            details={
                "spawned_by": "PIPELINE_REGRESSION_DISPATCHER",
                "regression_class": regression_class,
                "signal_name": "sr_catch_rate",
                "last_remediation_pr_number": pr_number,
            },
        )
    )
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_second_attempt_prompt_unchanged(db_session):
    """attempts==1: prompt has no prior-attempt context (existing behavior preserved)."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            attempts=1,
            last_remediation_pr_number=1234,
        )
    )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "PRIOR ATTEMPT CONTEXT" not in prompt


@pytest.mark.asyncio
async def test_fresh_spawn_prompt_has_no_recurrence_or_cross_layer_headers(db_session):
    """Fresh class with no history keeps the plain signal prompt."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert prompt.startswith(
        "Pipeline regression detected (sr_catch_rate / class=sr_catch_rate:correctness)."
    )
    assert "CROSS-LAYER REGRESSION DETECTED" not in prompt
    assert "RECURRENCE META-INVESTIGATION" not in prompt


@pytest.mark.asyncio
async def test_re_arm_spawn_uses_recurrence_meta_prompt(db_session):
    """Re-armed cases after MERGED transition get the recurrence meta prompt."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(hours=3)).isoformat()
    prior_task = await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        created_at=_utcnow() - timedelta(hours=7),
        pr_number=4321,
    )
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=0,
        last_remediation_task_id=prior_task.id,
        last_remediation_pr_number=4321,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
            "captured_at": merged_at,
        },
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 4321,
                "merged_at": merged_at,
            }
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await run_recovery_once(db_session, project.id)
    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.attempts == 1

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "RECURRENCE META-INVESTIGATION" in prompt
    assert "#4321" in prompt
    assert "PRIOR ATTEMPT CONTEXT" not in prompt
    for layer in (
        "CI_WORKFLOW",
        "AGENT_VM",
        "AGENT_VERB",
        "BASELINE_GATE",
        "REVIEWER_PASS",
    ):
        assert layer in prompt

    # Case-level escalation_reason stamped for operator audit trail.
    await db_session.refresh(case)
    assert case.escalation_reason == "escalated_recurrence"


@pytest.mark.asyncio
async def test_closed_case_with_recent_history_uses_recurrence_prompt(db_session):
    """A CLOSED case from the last 30 days re-spawns with recurrence guidance."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Merge 7 days ago, recovered 3 days ago → 4-day hold (above the 2-day fragile floor).
    merged_at = (_utcnow() - timedelta(days=7)).isoformat()
    recovered_at = (_utcnow() - timedelta(days=3)).isoformat()
    await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        created_at=_utcnow() - timedelta(days=7, minutes=5),
        pr_number=5678,
    )
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.CLOSED,
        hit_count=1,
        last_remediation_pr_number=5678,
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 5678,
                "merged_at": merged_at,
            },
            {
                "fingerprint": "sr_catch_rate",
                "recovered_at": recovered_at,
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "RECURRENCE META-INVESTIGATION" in prompt
    assert "#5678" in prompt
    assert "PRIOR ATTEMPT CONTEXT" not in prompt


@pytest.mark.asyncio
async def test_recurrence_prompt_layers_targeted_absent_renders_pending(db_session):
    """fix-log entries without layers_targeted render 'classification pending' and no mismatch block."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(hours=3)).isoformat()
    prior_task = await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        created_at=_utcnow() - timedelta(hours=7),
        pr_number=9001,
    )
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=0,
        last_remediation_task_id=prior_task.id,
        last_remediation_pr_number=9001,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
            "captured_at": merged_at,
        },
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 9001,
                "merged_at": merged_at,
            }
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await run_recovery_once(db_session, project.id)
    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "RECURRENCE META-INVESTIGATION" in prompt
    assert "layers: classification pending" in prompt
    assert "SYMPTOM-MISMATCH RECURRENCE" not in prompt


@pytest.mark.asyncio
async def test_recurrence_prompt_layers_targeted_overlap_no_mismatch(db_session):
    """When prior layers overlap with current obs layers, no mismatch block is emitted."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(hours=3)).isoformat()
    prior_task = await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        created_at=_utcnow() - timedelta(hours=7),
        pr_number=9100,
    )
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=0,
        last_remediation_task_id=prior_task.id,
        last_remediation_pr_number=9100,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
            "captured_at": merged_at,
        },
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 9100,
                "merged_at": merged_at,
                "layers_targeted": ["REVIEWER_PASS"],
            }
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await run_recovery_once(db_session, project.id)
    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "RECURRENCE META-INVESTIGATION" in prompt
    assert "layers: REVIEWER_PASS" in prompt
    assert "SYMPTOM-MISMATCH RECURRENCE" not in prompt


@pytest.mark.asyncio
async def test_recurrence_prompt_layers_targeted_disjoint_emits_mismatch_block(
    db_session,
):
    """When prior layers and current obs layers are disjoint, the SYMPTOM-MISMATCH block fires."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(hours=3)).isoformat()
    prior_task = await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        created_at=_utcnow() - timedelta(hours=7),
        pr_number=9200,
    )
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=0,
        last_remediation_task_id=prior_task.id,
        last_remediation_pr_number=9200,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
            "captured_at": merged_at,
        },
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 9200,
                "merged_at": merged_at,
                "layers_targeted": ["CI_WORKFLOW"],
            }
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await run_recovery_once(db_session, project.id)
    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "RECURRENCE META-INVESTIGATION" in prompt
    assert "SYMPTOM-MISMATCH RECURRENCE" in prompt
    # Names both sets
    assert "CI_WORKFLOW" in prompt
    assert "REVIEWER_PASS" in prompt
    # Calls out the un-defended layers explicitly
    assert "Difference (un-defended layers): REVIEWER_PASS" in prompt
    assert "Start your layer reasoning from the un-defended layers" in prompt
    # Per-attempt layer annotation reflects the prior layers
    assert "layers: CI_WORKFLOW" in prompt


@pytest.mark.asyncio
async def test_fragile_recurrence_meta_on_third_recurrence(db_session):
    """Third recurrence on a single case produces fragile-recurrence meta-investigation prompt."""
    from app.models import (
        Alert,
        AlertSeverity,
        RegressionCaseStatus,
        RegressionRemediationCase,
    )
    from app.pipeline_regression_dispatcher import (
        ESCALATION_REASON_FRAGILE_RECURRENCE_META,
    )

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(hours=3)).isoformat()
    # Seed three prior spawned tasks so _load_recurrence_prompt_context returns 3 cycles.
    for i in range(3):
        await _seed_dispatcher_spawn_history(
            db_session,
            project.id,
            regression_class="sr_catch_rate",
            created_at=_utcnow() - timedelta(hours=7 + i),
            pr_number=4400 + i,
        )

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=3,
        attempts=0,
        last_remediation_pr_number=4402,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
            "captured_at": merged_at,
        },
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 4400,
                "merged_at": merged_at,
            },
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 4401,
                "merged_at": merged_at,
            },
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 4402,
                "merged_at": merged_at,
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await run_recovery_once(db_session, project.id)
    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")

    # Must use the fragile-recurrence meta-investigation framing.
    assert "FRAGILE-RECURRENCE META-INVESTIGATION" in prompt
    assert "meta-investigation" in prompt.lower()

    # Must reference at least 2 distinct canonical layer classes.
    layer_mentions = [
        layer
        for layer in (
            "CI_WORKFLOW",
            "AGENT_VM",
            "AGENT_VERB",
            "BASELINE_GATE",
            "REVIEWER_PASS",
        )
        if layer in prompt
    ]
    assert len(layer_mentions) >= 2, (
        f"Expected >=2 layer classes, got: {layer_mentions}"
    )

    # Case escalation reason must be set.
    await db_session.refresh(case)
    assert case.escalation_reason == ESCALATION_REASON_FRAGILE_RECURRENCE_META

    # Alert level must be INFO (not WARNING — expected, not anomalous behavior).
    alert = (
        (
            await db_session.execute(
                select(Alert)
                .where(Alert.project_id == project.id)
                .order_by(Alert.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert alert is not None
    assert alert.severity == AlertSeverity.INFO


@pytest.mark.asyncio
async def test_third_attempt_prompt_enriched(db_session):
    """attempts==2: spawned prompt includes prior-attempt context with PR and post-merge value."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=2,
            attempts=2,
            last_remediation_pr_number=5678,
            signal_snapshot_at_merge={
                "current_rate": 0.65,
                "prior_rate": 0.80,
                "captured_at": "2026-01-01T00:00:00",
            },
        )
    )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "PRIOR ATTEMPT CONTEXT" in prompt
    assert "#5678" in prompt
    assert "65.0%" in prompt
    assert (
        "different angle" in prompt.lower()
        or "different contributing" in prompt.lower()
    )


@pytest.mark.asyncio
async def test_prior_attempt_no_pr_no_pr_reference(db_session):
    """attempts>=2 but no prior PR: enriched prompt does not mention a PR number."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=2,
            attempts=2,
            last_remediation_pr_number=None,
        )
    )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "PRIOR ATTEMPT CONTEXT" in prompt
    assert "PR #" not in prompt


@pytest.mark.asyncio
async def test_recovery_scan_bucket_class_closes_when_all_recovered(db_session):
    """Bucket-level MERGED case closes when all constituent signals recover."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    # Both categories initially recovered (prior ≈ current).
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness",
            prior=0.80,
            current=0.80,
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    merged_at = (_utcnow() - timedelta(minutes=20)).isoformat()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        signal_snapshot_at_merge={"captured_at": merged_at},
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
        fingerprint_to_fix_log=[],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


# ---------------------------------------------------------------------------
# SR blind-spot signal evaluator tests
# ---------------------------------------------------------------------------


def _make_sr_blind_spot_task(
    project_id,
    *,
    category: str = "correctness",
    task_type: str = "create",
    miss: bool = True,
    true_positive: bool = False,
    created_at=None,
) -> Task:
    """Build a COMPLETED code-change task with SR vs reviewer data.

    miss=True: reviewer caught `category` but SR passed it (miss in SR).
    true_positive=True: reviewer caught `category` AND SR also flagged it
      (contributes to denominator with 0 miss — used for recovery tests to
      get denom >= _MIN_SR_SAMPLE while keeping miss rate = 0%).
    """
    from testing.factories import _utcnow as _fac_utcnow

    ts = created_at or _fac_utcnow()
    if miss:
        reviewer_findings = [
            {
                "category": category,
                "severity": "HIGH",
                "description": f"Reviewer caught missing {category} check in func()",
            }
        ]
        check_results = {category: {"passed": True}}  # SR passed — miss
    elif true_positive:
        reviewer_findings = [
            {
                "category": category,
                "severity": "HIGH",
                "description": f"Reviewer confirmed {category} issue",
            }
        ]
        check_results = {category: {"passed": False}}  # SR also flagged — tp
    else:
        reviewer_findings = []
        check_results = {category: {"passed": True}}
    sro = {
        "verdict": "pass",
        "check_results": check_results,
        "blind_spots": [],
        "reviewer_findings": reviewer_findings,
    }
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": task_type, "prompt": "do the thing", "base_branch": "main"}],
        context={"_self_review_outcome": sro},
        created_at=ts,
    )


def _get_investigate_task(tasks):
    """Return the first investigate task from a list (filters out seed tasks)."""
    for t in tasks:
        if (t.first_payload or {}).get("type") == "investigate":
            return t
    return None


@pytest.mark.asyncio
async def test_sr_blind_spot_disagreement_trips_both_windows(db_session):
    """Both 7d windows exceed _TREND_DELTA_THRESHOLD with adequate sample → spawns sr_blind_spot:{cat}."""
    from app.playbook_learning import _MIN_SR_SAMPLE
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                category="correctness",
                miss=True,
                created_at=now - timedelta(days=3),
            )
        )
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                category="correctness",
                miss=True,
                created_at=now - timedelta(days=10),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    payload = task.first_payload or {}
    assert payload.get("type") == "investigate"
    ctx = task.context or {}
    assert ctx["regression_class"] == "sr_blind_spot:correctness"
    assert ctx["signal_name"] == "sr_blind_spot"
    prompt = payload.get("prompt", "")
    assert "correctness" in prompt
    assert "reviewer finding" in prompt.lower()


@pytest.mark.asyncio
async def test_sr_blind_spot_disagreement_one_window_only_no_spawn(db_session):
    """Only current window has miss rate > threshold (prior window has no samples) → no spawn."""
    from app.playbook_learning import _MIN_SR_SAMPLE
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # Current window: all miss (rate = 100%)
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=True,
                created_at=now - timedelta(days=3),
            )
        )
    # Prior window: no reviewer findings → p_denom = 0 < _MIN_SR_SAMPLE → suppressed
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=False,
                created_at=now - timedelta(days=10),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_sr_blind_spot_disagreement_below_sample_floor_no_spawn(db_session):
    """Current window denominator < _MIN_SR_SAMPLE → signal suppressed (no spawn)."""
    from app.playbook_learning import _MIN_SR_SAMPLE
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # Current window: _MIN_SR_SAMPLE - 1 miss tasks (denominator below floor)
    for _ in range(_MIN_SR_SAMPLE - 1):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=True,
                created_at=now - timedelta(days=3),
            )
        )
    # Prior window: adequate samples
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=True,
                created_at=now - timedelta(days=10),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_trips_at_count_floor(db_session):
    """Theme count >= _SR_BLIND_SPOT_THEME_FLOOR → spawns sr_blind_spot:theme:* investigate."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    theme_text = "edge case not tested in the new path"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [theme_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    ctx = task.context or {}
    assert ctx["regression_class"].startswith("sr_blind_spot:theme:")
    assert ctx["signal_name"] == "sr_blind_spot"
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "edge case not" in prompt


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_below_floor_no_spawn(db_session):
    """Theme count < _SR_BLIND_SPOT_THEME_FLOOR → no spawn."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    theme_text = "rare edge case in parser logic"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR - 1):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [theme_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_prompt_has_layer_enumeration_block(db_session):
    """Theme spawn prompt includes the META-INVESTIGATION layer-enumeration block on first dispatch."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    theme_text = "missing null check in async handler path"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [theme_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "META-INVESTIGATION" in prompt
    assert "Enumerate ALL FIVE canonical layers explicitly" in prompt
    for layer in (
        "CI_WORKFLOW",
        "AGENT_VM",
        "AGENT_VERB",
        "BASELINE_GATE",
        "REVIEWER_PASS",
    ):
        assert layer in prompt


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_prompt_includes_verbatim_examples(db_session):
    """Theme spawn prompt contains verbatim example blind_spot entries with task IDs."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    theme_text = "unhandled edge case in the new feature code"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [theme_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    prompt = (task.first_payload or {}).get("prompt", "")
    # Verbatim examples are formatted as "Example (task <id> / <type>): <text>".
    assert "Example (task" in prompt
    assert theme_text in prompt


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_symptoms_by_layer_populated_from_examples(
    db_session,
):
    """symptoms_by_layer is populated from example texts when classify_layers matches them.

    Uses a theme text matching the CI_WORKFLOW "github actions.*fail" pattern so
    that a layer beyond the default REVIEWER_PASS (from stage='review') is recorded.
    """
    from app.models import RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # Matches _CI_WORKFLOW_PATTERNS: "github actions.*fail"
    theme_text = "github actions failed for the ci workflow check step"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [theme_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class.like(
                        "sr_blind_spot:theme:%"
                    ),
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    sbl = case.symptoms_by_layer or {}
    # REVIEWER_PASS is always tagged for sr_blind_spot signals (stage='review').
    assert "REVIEWER_PASS" in sbl
    # CI_WORKFLOW is tagged because the example text matches "github actions.*fail".
    assert "CI_WORKFLOW" in sbl


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_cooldown_suppresses_spawn(db_session):
    """Per-class cooldown blocks a second theme spawn within the 6h window after the first terminates."""
    from app.pipeline_regression_dispatcher import (
        _REGRESSION_COOLDOWN,
        _SR_BLIND_SPOT_THEME_FLOOR,
    )
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    theme_text = "scope drift detected in the new code path here"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [theme_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    first = await run_once(db_session, project.id)
    assert first == 1

    # Mark the spawned investigate task as COMPLETED — no longer in-flight.
    spawned_task = _get_investigate_task(
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert spawned_task is not None
    spawned_task.status = TaskStatus.COMPLETED
    db_session.add(spawned_task)
    await db_session.commit()

    # Cooldown still active — should suppress even though task is COMPLETED.
    second = await run_once(db_session, project.id)
    assert second == 0

    # Backdate the spawn beyond the cooldown window.
    spawned_task.created_at = _ts_now() - _REGRESSION_COOLDOWN - timedelta(minutes=5)
    db_session.add(spawned_task)
    await db_session.commit()

    # Cooldown elapsed — a fresh spawn should fire.
    third = await run_once(db_session, project.id)
    assert third == 1


@pytest.mark.asyncio
async def test_sr_blind_spot_disallowed_theme_does_not_spawn(db_session):
    """Disallowed blind_spot theme at/above floor does not spawn an investigation.

    Verifies the dispatcher's is_disallowed_blind_spot gate: even when a
    forbidden theme (e.g. 'did not run docker pytest') meets _SR_BLIND_SPOT_THEME_FLOOR,
    no sr_blind_spot:theme:* task is spawned.
    """
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # Use a disallowed theme text that matches DISALLOWED_BLIND_SPOT_PATTERNS.
    disallowed_theme = "did not run docker pytest"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [disallowed_theme],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0, (
        "Dispatcher must not spawn an investigation for a disallowed blind_spot theme"
    )


@pytest.mark.asyncio
async def test_sr_blind_spot_ingestion_filter_prevents_theme_spawn(db_session):
    """After ingestion filter, disallowed blind_spots are absent → theme aggregator
    has zero count → no spawn.

    Simulates what happens when filter_disallowed_blind_spots is applied at write
    time: the stored _self_review_outcome.blind_spots is empty, so the theme
    aggregator never accumulates the disallowed theme above the floor.
    """
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.sr_blind_spots import filter_disallowed_blind_spots
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        raw_sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            # Disallowed entry that the ingestion filter would remove.
            "blind_spots": ["did not run the full backend test suite"],
            "reviewer_findings": [],
        }
        # Apply the same filter used at ingestion (agent_status.py).
        filtered_sro = filter_disallowed_blind_spots(dict(raw_sro))
        assert filtered_sro["blind_spots"] == [], (
            "Filter must remove the disallowed entry"
        )
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": filtered_sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0, (
        "Ingestion-filtered blind_spots must not accumulate into a spawnable theme"
    )


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_stale_assertion_triage_context_in_prompt(db_session):
    """likely_stale_assertion theme → spawned prompt includes triage context."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # This text produces a theme with ≥2 stale-assertion signals:
    # Signal 1: "agent/src/prompt.test.ts" matches the test-file pattern.
    # Signal 2: "expects ordering" matches the assertion-phrase list.
    stale_text = "agent/src/prompt.test.ts now expects ordering of install commands"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [stale_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "Triage:" in prompt, "Stale-assertion triage context must appear in prompt"
    assert "stale" in prompt.lower(), "Prompt should mention stale assertion"


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_prompt_regression_no_triage_context(db_session):
    """likely_prompt_regression theme → spawned prompt has no triage context appended."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # This text fires zero stale-assertion signals: no test-file path, no assertion keywords.
    regression_text = "missing error handling for null input edge case"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [regression_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "Triage:" not in prompt, (
        "Prompt-regression theme must not include triage context"
    )


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_ambiguous_triage_context_in_prompt(db_session):
    """ambiguous theme (1 signal) → spawned prompt includes triage context."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # Signal 1 only: "test_utils.py" matches test-file pattern.
    # No assertion keywords → ambiguous (1 signal).
    ambiguous_text = "test_utils.py behavior edge cases not covered by suite"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [ambiguous_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "Triage:" in prompt, "Ambiguous triage context must appear in prompt"


@pytest.mark.asyncio
async def test_sr_blind_spot_dedup_inflight_blocks_second_spawn(db_session):
    """Second run_once with an in-flight sr_blind_spot investigation → no duplicate spawn."""
    from app.playbook_learning import _MIN_SR_SAMPLE
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=True,
                created_at=now - timedelta(days=3),
            )
        )
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=True,
                created_at=now - timedelta(days=10),
            )
        )
    await db_session.commit()

    first = await run_once(db_session, project.id)
    assert first == 1

    second = await run_once(db_session, project.id)
    assert second == 0

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    investigate_tasks = [
        t for t in all_tasks if (t.first_payload or {}).get("type") == "investigate"
    ]
    assert len(investigate_tasks) == 1


@pytest.mark.asyncio
async def test_sr_blind_spot_recovery_scan_closes_recovered(db_session):
    """run_recovery_once closes a MERGED sr_blind_spot case when rate drops below threshold.

    Seeds the current 7d window with true-positive tasks (reviewer confirmed
    what SR also flagged) so the denominator is >= _MIN_SR_SAMPLE but miss=0 →
    rate=0% → signal not tripped → case is CLOSED.
    """
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.playbook_learning import _MIN_SR_SAMPLE
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # True-positive tasks: reviewer flagged AND SR flagged (tp) — denom >= floor, miss=0.
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                category="correctness",
                miss=False,
                true_positive=True,
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    merged_at = (now - timedelta(minutes=20)).isoformat()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_blind_spot:correctness",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        signal_snapshot_at_merge={"captured_at": merged_at},
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.80,
            "signal_name": "sr_blind_spot",
        },
        fingerprint_to_fix_log=[],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


@pytest.mark.asyncio
async def test_sr_blind_spot_deploy_monitoring_defers(db_session):
    """Sr_blind_spot signal during MONITORING window → 0 spawns, deferral Alert emitted."""
    from app.models import Alert, DeploymentState, DeploymentStatus
    from app.playbook_learning import _MIN_SR_SAMPLE
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.MONITORING,
            deployed_at=_ts_now(),
        )
    )
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=True,
                created_at=now - timedelta(days=3),
            )
        )
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(
            _make_sr_blind_spot_task(
                project.id,
                miss=True,
                created_at=now - timedelta(days=10),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    investigate_tasks = [
        t for t in all_tasks if (t.first_payload or {}).get("type") == "investigate"
    ]
    assert investigate_tasks == []

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert any("monitoring" in (a.title or "").lower() for a in alerts)


# ---------------------------------------------------------------------------
# MTTD — signal_first_seen_at population tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_first_seen_at_set_for_sr_catch_rate(db_session):
    """signal_first_seen_at is populated when a new SR catch-rate case is opened."""
    from app.models import RegressionRemediationCase
    from app.time_utils import _utcnow

    before = _utcnow()
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class == "sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.signal_first_seen_at is not None
    # For SR catch-rate there is no durable first_seen timestamp, so the
    # dispatcher records ~now (detection time ≈ spawn time).
    assert case.signal_first_seen_at >= before


@pytest.mark.asyncio
async def test_signal_first_seen_at_uses_anomaly_first_seen(db_session):
    """signal_first_seen_at reflects the anomaly's first_seen, enabling accurate MTTD."""
    import app.shared_state as _ss
    from app.models import RegressionRemediationCase
    from app.time_utils import _utcnow

    now = _utcnow()
    # first_seen is 2 days before spawn — MTTD should be ~2 days.
    first_seen_dt = now - timedelta(days=2)
    first_seen_iso = first_seen_dt.isoformat()

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "vm_provision_failure_mttd"
    _ss.persistent_anomalies[str(project.id)] = {
        fingerprint: {
            "occurrence_count": 5,
            "last_seen": now.isoformat(),
            "first_seen": first_seen_iso,
            "severity": "high",
        },
    }
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1

        case = (
            (
                await db_session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project.id,
                        RegressionRemediationCase.regression_class
                        == f"anomaly:{fingerprint}",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert case is not None
        assert case.signal_first_seen_at is not None
        # Should match the anomaly's first_seen, not spawn time.
        delta = abs((case.signal_first_seen_at - first_seen_dt).total_seconds())
        assert delta < 2, (
            f"signal_first_seen_at mismatch: {case.signal_first_seen_at} vs {first_seen_dt}"
        )
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_mttd_non_zero_across_ticks(db_session):
    """signal_first_seen_at persists across ticks so MTTD is non-zero on re-spawn."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc=_design_doc_with_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await run_once(db_session, project.id)

    # The case row is now the durable source of truth for the first-trip
    # time. Backdate it to simulate the signal having tripped 3h before
    # the first spawn — a worker restart between trip and spawn would
    # produce the same row state.
    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.signal_first_seen_at is not None
    earlier = case.signal_first_seen_at - timedelta(hours=3)
    case.signal_first_seen_at = earlier
    case.signal_first_crossed_at = earlier
    case.status = RegressionCaseStatus.CLOSED
    prior_task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    prior_task.created_at = prior_task.created_at - timedelta(hours=7)
    # Clear the dedup gate (_in_flight_for_class) by completing the prior
    # investigate task — otherwise _maybe_spawn returns None on the dedup
    # path and no new ACTIVE case is created on the second run_once.
    prior_task.status = TaskStatus.COMPLETED
    await db_session.commit()

    await run_once(db_session, project.id)

    cases = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    active_case = next(
        (c for c in cases if c.status == RegressionCaseStatus.ACTIVE), None
    )
    assert active_case is not None
    assert active_case.signal_first_seen_at is not None
    delta = abs((active_case.signal_first_seen_at - earlier).total_seconds())
    assert delta < 2, "signal_first_seen_at should reflect the earliest tracked trip"
    assert (
        active_case.created_at - active_case.signal_first_seen_at
    ).total_seconds() > 0


@pytest.mark.asyncio
async def test_mttd_tracker_cleared_on_recovery(db_session):
    """run_recovery_once clears the case's signal_healthy_since_at on close."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc=_design_doc_with_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await run_once(db_session, project.id)

    # Remove regression so signal reads as recovered.
    project.design_doc = {}
    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id
                )
            )
        )
        .scalars()
        .first()
    )
    # Seed a stale healthy_since timestamp on the case row — the recovery
    # close path must NULL it out so a re-escalation cannot inherit a stale
    # auto-close clock from a prior lifecycle.
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    case.status = RegressionCaseStatus.MERGED
    case.signal_snapshot_at_merge = {
        "captured_at": (_utcnow() - timedelta(hours=1)).isoformat()
    }
    await db_session.commit()
    await db_session.refresh(project)

    await run_recovery_once(db_session, project.id)

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.signal_healthy_since_at is None, (
        "recovery clock must be cleared when signal recovers"
    )


# ---------------------------------------------------------------------------
# Divergence detector — investigations-diverged escalation
# ---------------------------------------------------------------------------


def _diagnosis_payload(
    *, root_cause: str, files: list[str], confidence: str = "high"
) -> dict:
    return {
        "summary": "synthetic diagnosis",
        "root_cause": root_cause,
        "recommended_fix": "apply the fix",
        "confidence": confidence,
        "severity": "high",
        "affected_files": [{"path": p, "issue": "x"} for p in files],
    }


async def _seed_pipeline_regression_case(
    db_session,
    project_id,
    *,
    attempts: int,
    regression_class: str = "sr_catch_rate",
):
    """Create a RegressionRemediationCase mirroring the dispatcher's spawn shape so
    divergence detection has a real row to compare against.
    """
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    case = RegressionRemediationCase(
        project_id=project_id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        attempts=attempts,
        hit_count=attempts + 1,
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)
    return case


async def _make_prior_investigation(
    db_session,
    project_id,
    *,
    regression_class: str,
    plan_output: dict,
):
    prior = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate", "prompt": "old", "base_branch": "main"}],
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": regression_class,
        },
        plan_output=plan_output,
    )
    db_session.add(prior)
    await db_session.commit()
    await db_session.refresh(prior)
    return prior


@pytest.mark.asyncio
async def test_divergence_converging_diagnoses_do_not_escalate(db_session, mocker):
    """Two converging diagnoses at attempts=2 → no escalation, remediation spawns."""
    from app.models import (
        Alert,
        ESCALATION_REASON_INVESTIGATIONS_DIVERGED,
        RegressionCaseStatus,
    )
    from app.task_completion import _handle_investigation_completion

    mocker.patch("app.task_completion.broadcast_to_project", new=mocker.AsyncMock())

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _seed_pipeline_regression_case(db_session, project.id, attempts=2)
    prior = await _make_prior_investigation(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        plan_output=_diagnosis_payload(
            root_cause="prompt drift in self-review checklist eroded coverage",
            files=["agent/src/prompts/self-review.ts", "agent/src/prompt.ts"],
        ),
    )
    assert prior is not None  # fixture used as side-effect, silence linters

    current = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate", "prompt": "new", "base_branch": "main"}],
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    diagnosis = _diagnosis_payload(
        root_cause="prompt drift in the self-review checklist hurt coverage further",
        files=["agent/src/prompts/self-review.ts", "agent/src/prompt.ts"],
    )
    await _handle_investigation_completion(
        current,
        SimpleNamespace(logs=json.dumps(diagnosis)),
        db_session,
    )
    await db_session.commit()

    await db_session.refresh(case)
    assert case.status != RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason != ESCALATION_REASON_INVESTIGATIONS_DIVERGED

    remediation = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == current.id)
            )
        )
        .scalars()
        .first()
    )
    assert remediation is not None, "converging diagnoses must still spawn remediation"

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert not any(
        "investigations diverged" in (a.message or "").lower() for a in alerts
    )


@pytest.mark.asyncio
async def test_divergence_diverging_diagnoses_escalate(db_session, mocker):
    """Two diverging diagnoses at attempts=2 → escalate; remediation spawn skipped."""
    from app.models import (
        Alert,
        ESCALATION_REASON_INVESTIGATIONS_DIVERGED,
        RegressionCaseStatus,
    )
    from app.task_completion import _handle_investigation_completion

    mocker.patch("app.task_completion.broadcast_to_project", new=mocker.AsyncMock())

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _seed_pipeline_regression_case(db_session, project.id, attempts=2)
    await _make_prior_investigation(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        plan_output=_diagnosis_payload(
            root_cause="prompt drift in self-review checklist eroded coverage",
            files=["agent/src/prompts/self-review.ts"],
        ),
    )

    current = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate", "prompt": "new", "base_branch": "main"}],
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    diagnosis = _diagnosis_payload(
        root_cause="latency spike from upstream provider throttling external API quota",
        files=["backend/app/orchestrator.py", "backend/app/services.py"],
    )
    pre_alert_count = len(
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )

    await _handle_investigation_completion(
        current,
        SimpleNamespace(logs=json.dumps(diagnosis)),
        db_session,
    )
    await db_session.commit()

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason == ESCALATION_REASON_INVESTIGATIONS_DIVERGED
    assert case.needs_manual_intervention_at is not None

    remediation = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == current.id)
            )
        )
        .scalars()
        .first()
    )
    assert remediation is None, "diverged diagnoses must skip remediation spawn"

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) > pre_alert_count
    assert any("investigations diverged" in (a.message or "").lower() for a in alerts)


@pytest.mark.asyncio
async def test_re_arm_third_attempt_stamps_attempts_exhausted():
    """Direct unit test: re_arm at attempts==2 escalates and stamps escalation_reason."""
    from app.models import (
        ESCALATION_REASON_ATTEMPTS_EXHAUSTED,
        RegressionCaseStatus,
        RegressionRemediationCase,
    )

    case = RegressionRemediationCase(
        project_id=uuid.uuid4(),
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        attempts=2,
        hit_count=1,
    )
    case.re_arm()
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason == ESCALATION_REASON_ATTEMPTS_EXHAUSTED
    assert case.needs_manual_intervention_at is not None

    # Idempotent: re_arm on an already-NMI case does not overwrite the reason.
    case.re_arm()
    assert case.escalation_reason == ESCALATION_REASON_ATTEMPTS_EXHAUSTED


@pytest.mark.asyncio
async def test_watchdog_stamps_stale_merged_escalation_reason(db_session):
    """Watchdog stale-MERGED escalation stamps escalation_reason='stale_merged'."""
    from app.models import (
        ESCALATION_REASON_INVESTIGATIONS_DIVERGED,
        ESCALATION_REASON_STALE_MERGED,
        RegressionCaseStatus,
    )

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_hours = int(_WATCHDOG_MERGED_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.MERGED,
        hours_stale=stale_hours,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason == ESCALATION_REASON_STALE_MERGED
    assert case.escalation_reason != ESCALATION_REASON_INVESTIGATIONS_DIVERGED


@pytest.mark.asyncio
async def test_divergence_attempts_below_two_does_not_trigger(db_session, mocker):
    """First investigation completing (attempts<2) never enters divergence detection."""
    from app.models import (
        ESCALATION_REASON_INVESTIGATIONS_DIVERGED,
        RegressionCaseStatus,
    )
    from app.task_completion import _handle_investigation_completion

    mocker.patch("app.task_completion.broadcast_to_project", new=mocker.AsyncMock())

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _seed_pipeline_regression_case(db_session, project.id, attempts=0)

    current = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "investigate", "prompt": "new", "base_branch": "main"}],
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    diagnosis = _diagnosis_payload(
        root_cause="completely fresh diagnosis with no prior to compare",
        files=["backend/app/orchestrator.py"],
    )
    await _handle_investigation_completion(
        current,
        SimpleNamespace(logs=json.dumps(diagnosis)),
        db_session,
    )
    await db_session.commit()

    await db_session.refresh(case)
    assert case.status != RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason != ESCALATION_REASON_INVESTIGATIONS_DIVERGED

    remediation = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == current.id)
            )
        )
        .scalars()
        .first()
    )
    assert remediation is not None


# ---------------------------------------------------------------------------
# Strategic cycle stall signal tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_stall_spawns_when_stall_breached_and_project_active(
    db_session,
):
    """Spawns when no strategic task completed in stall window + project is active."""
    from app.pipeline_regression_dispatcher import _STRATEGIC_CYCLE_STALL_HOURS

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Prior strategic cycle (older than stall window) proves project maturity.
    prior_strategic = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "strategic_review", "prompt": "x", "base_branch": "main"}],
        context={},
    )
    db_session.add(prior_strategic)
    # Non-strategic activity within the stall window (project is currently active).
    active_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        context={},
    )
    db_session.add(active_task)
    await db_session.commit()

    prior_strategic.created_at = _utcnow() - timedelta(
        hours=_STRATEGIC_CYCLE_STALL_HOURS + 2
    )
    active_task.created_at = _utcnow() - timedelta(
        hours=_STRATEGIC_CYCLE_STALL_HOURS - 1
    )
    db_session.add(prior_strategic)
    db_session.add(active_task)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string()
                    == "strategic_cycle_stall",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    assert (tasks[0].first_payload or {}).get("type") == "investigate"


@pytest.mark.asyncio
async def test_strategic_stall_no_spawn_when_project_idle(db_session):
    """Does NOT spawn when no non-strategic activity exists in the stall window."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No tasks at all — project is idle.
    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_strategic_stall_no_spawn_when_recent_cycle_exists(db_session):
    """Does NOT spawn when a strategic_review completed within the stall window."""
    from app.pipeline_regression_dispatcher import _STRATEGIC_CYCLE_STALL_HOURS

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Prior strategic cycle establishes project maturity.
    prior_strategic = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "strategic_review", "prompt": "x", "base_branch": "main"}],
        context={},
    )
    db_session.add(prior_strategic)
    # Recent strategic_review (within the window) — no stall.
    recent_strategic = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "strategic_review", "prompt": "x", "base_branch": "main"}],
        context={},
    )
    db_session.add(recent_strategic)
    # Non-strategic activity so the project is not idle.
    active_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        context={},
    )
    db_session.add(active_task)
    await db_session.commit()

    prior_strategic.created_at = _utcnow() - timedelta(
        hours=_STRATEGIC_CYCLE_STALL_HOURS + 2
    )
    recent_strategic.created_at = _utcnow() - timedelta(
        hours=_STRATEGIC_CYCLE_STALL_HOURS - 1
    )
    active_task.created_at = _utcnow() - timedelta(
        hours=_STRATEGIC_CYCLE_STALL_HOURS - 2
    )
    db_session.add(prior_strategic)
    db_session.add(recent_strategic)
    db_session.add(active_task)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_strategic_stall_respects_cooldown(db_session):
    """After a stall spawn, the 6h cooldown prevents a second spawn."""
    from app.pipeline_regression_dispatcher import _STRATEGIC_CYCLE_STALL_HOURS

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_strategic = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "strategic_review", "prompt": "x", "base_branch": "main"}],
        context={},
    )
    db_session.add(prior_strategic)
    active_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        context={},
    )
    db_session.add(active_task)
    await db_session.commit()

    prior_strategic.created_at = _utcnow() - timedelta(
        hours=_STRATEGIC_CYCLE_STALL_HOURS + 2
    )
    active_task.created_at = _utcnow() - timedelta(
        hours=_STRATEGIC_CYCLE_STALL_HOURS - 1
    )
    db_session.add(prior_strategic)
    db_session.add(active_task)
    await db_session.commit()

    first = await run_once(db_session, project.id)
    assert first == 1

    # Second call within cooldown window — must not double-spawn.
    second = await run_once(db_session, project.id)
    assert second == 0

    rows = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string()
                    == "strategic_cycle_stall",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Dispatcher cycle record emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_record_emitted_on_tick(db_session):
    """run_once() emits a cycle record into the durable table on every call."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    records = await get_dispatcher_cycle_records(db_session, project_id=str(project.id))
    assert len(records) >= 1
    rec = records[-1]
    assert rec["project_id"] == str(project.id)
    assert "cycle_started_at" in rec
    assert "cycle_finished_at" in rec
    assert rec["spawns_emitted"] == 1
    rj = rec["rejections_by_reason"]
    assert rj["dedup"] == 0
    assert rj["cooldown"] == 0
    assert rj["system_cap"] == 0


@pytest.mark.asyncio
async def test_rejection_reason_dedup_attributed(db_session):
    """When an in-flight task blocks a second spawn the rejection is counted as dedup, not cooldown."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await run_once(db_session, project.id)
    assert first == 1

    # Run again while the spawned task is still in QUEUED — dedup should fire.
    second = await run_once(db_session, project.id)
    assert second == 0

    records = await get_dispatcher_cycle_records(db_session, project_id=str(project.id))
    last_rec = records[-1]
    rj = last_rec["rejections_by_reason"]
    assert rj["dedup"] >= 1, "dedup rejection not recorded"
    assert rj["cooldown"] == 0, "rejection wrongly attributed to cooldown"


@pytest.mark.asyncio
async def test_cycle_record_survives_fresh_session(db_session):
    """Cycle records written in one session are visible from a fresh session.

    Demonstrates the durable-storage requirement of this PR: a worker that
    crashes mid-cycle must not lose throughput history that the admin
    health endpoint and the zero-spawn-streak watchdog depend on.

    The fresh-session check only runs on the Postgres test backend; the
    SQLite in-memory test fixture re-creates schema per-test in an isolated
    connection so a cross-session read cannot demonstrate durability.  The
    same-session path is exercised on both backends so the test is not a
    no-op on SQLite — it still confirms the row was written.
    """
    from testing.conftest import TestingSessionLocal, USE_POSTGRES

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    # Same-session readback (works on both SQLite and Postgres).
    same_session_rows = (
        (
            await db_session.execute(
                select(DispatcherCycleRecord).where(
                    DispatcherCycleRecord.project_id == project.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(same_session_rows) >= 1
    assert any(r.spawns_emitted == 1 for r in same_session_rows)

    if USE_POSTGRES:
        # Cross-session readback on a real DB — proves the cycle record
        # actually committed and is visible to a worker that did not write it.
        async with TestingSessionLocal() as fresh:
            rows = (
                (
                    await fresh.execute(
                        select(DispatcherCycleRecord).where(
                            DispatcherCycleRecord.project_id == project.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) >= 1
            assert any(r.spawns_emitted == 1 for r in rows)


@pytest.mark.asyncio
async def test_health_endpoint_returns_no_data_on_empty_db(db_session):
    """Admin health endpoint returns zeros / empty arrays on a cold start.

    No DispatcherCycleRecord rows + no RegressionRemediationCase rows must
    not raise — the UI displays 'no data' rather than crashing.
    """
    from app.routers.admin_digest import get_regression_dispatcher_health

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    data = await get_regression_dispatcher_health(
        project_id=project.id,
        window_hours=24,
        session=db_session,
        current_user={},
    )
    agg = data["aggregate"]
    assert agg["cycles"] == 0
    assert agg["spawns"] == 0
    assert agg["signals_tripped"] == 0
    assert agg["signals_evaluated"] == 0
    assert agg["rejections"]["cooldown"] == 0
    assert agg["rejections"]["dedup"] == 0
    assert agg["rejections"]["system_cap"] == 0
    assert agg["rejections"]["manual_intervention_suppressed"] == 0
    assert data["per_class"] == []
    assert len(data["spawns_sparkline"]) == 24


@pytest.mark.asyncio
async def test_signal_healthy_since_persists_across_worker_restart(db_session):
    """signal_healthy_since_at is preserved across simulated worker restart.

    Two ``_auto_close_recovered_manual_intervention`` ticks: between them
    we mutate the case row to simulate a worker restart that loses any
    process-local state. The unattended-window check must still use the
    persisted timestamp on the second tick instead of resetting to ``now``.
    """
    from app.models import RegressionCaseStatus
    from app.pipeline_regression_dispatcher import (
        _auto_close_recovered_manual_intervention,
    )

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )

    # Tick 1: signal is healthy — populates case.signal_healthy_since_at.
    await _auto_close_recovered_manual_intervention(db_session, project.id)
    await db_session.refresh(case)
    assert case.signal_healthy_since_at is not None
    first_seen = case.signal_healthy_since_at

    # Backdate 30h to simulate the window having elapsed BETWEEN ticks
    # while the worker was down.  A fresh worker reading the column must
    # see the original timestamp, not its own ``now``.
    backdated = _utcnow() - timedelta(hours=30)
    case.signal_healthy_since_at = backdated
    db_session.add(case)
    await db_session.commit()

    # Tick 2 (different worker would read the same row): auto-close fires
    # because the persisted timestamp puts us past the 24h window.
    marginal_closed, confirmed_closed = await _auto_close_recovered_manual_intervention(
        db_session, project.id
    )
    # No signal_snapshot_at_merge → not classified marginal → confirmed close.
    assert (marginal_closed, confirmed_closed) == (0, 1)
    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    # Cleared on close — never carries forward into a re-escalation.
    assert case.signal_healthy_since_at is None
    assert first_seen != backdated  # sanity: backdating actually mutated it


# ---------------------------------------------------------------------------
# signal_first_crossed_at tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_first_crossed_at_set_on_new_case(db_session):
    """signal_first_crossed_at is populated when a new RegressionRemediationCase is created."""
    from app.models import RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    before = _utcnow()
    spawned = await run_once(db_session, project.id)
    after = _utcnow()
    assert spawned == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.signal_first_crossed_at is not None
    crossed = case.signal_first_crossed_at
    if crossed.tzinfo is not None:
        crossed = crossed.replace(tzinfo=None)
    assert before <= crossed <= after


@pytest.mark.asyncio
async def test_signal_first_crossed_at_preserved_for_active_case(db_session):
    """signal_first_crossed_at is NOT reset when an ACTIVE case spawns again after cooldown."""
    from app.models import RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First spawn — establishes signal_first_crossed_at.
    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                )
            )
        )
        .scalars()
        .first()
    )
    original_crossed_at = case.signal_first_crossed_at
    assert original_crossed_at is not None

    # Mark the in-flight task COMPLETED and backdate it past the cooldown.
    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    task.status = TaskStatus.COMPLETED
    task.created_at = _utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5)
    db_session.add(task)
    await db_session.commit()

    # Second spawn — case is still ACTIVE, crossed_at should be preserved.
    spawned2 = await run_once(db_session, project.id)
    assert spawned2 == 1

    await db_session.refresh(case)
    assert case.signal_first_crossed_at == original_crossed_at


@pytest.mark.asyncio
async def test_signal_first_crossed_at_reset_on_reopen_from_closed(db_session):
    """signal_first_crossed_at resets to now when a CLOSED case re-trips."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed a pre-existing CLOSED case with an old signal_first_crossed_at.
    old_time = _utcnow() - timedelta(days=5)
    existing_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.CLOSED,
        hit_count=1,
        signal_first_crossed_at=old_time,
        signal_snapshot_at_spawn={},
        signal_snapshot_at_merge={},
        fingerprint_to_fix_log=[],
    )
    db_session.add(existing_case)
    await db_session.commit()

    before = _utcnow()
    spawned = await run_once(db_session, project.id)
    after = _utcnow()
    assert spawned == 1

    await db_session.refresh(existing_case)
    assert existing_case.signal_first_crossed_at is not None
    crossed = existing_case.signal_first_crossed_at
    if crossed.tzinfo is not None:
        crossed = crossed.replace(tzinfo=None)
    # Must be a fresh timestamp (not the old_time from 5 days ago).
    assert before <= crossed <= after


# ---------------------------------------------------------------------------
# Background-service crash case — run_recovery_once integration
# ---------------------------------------------------------------------------


async def _make_crash_case(
    db_session,
    project_id,
    *,
    service_name: str,
    attempts: int = 0,
    merged_at_offset_minutes: int = 20,
) -> RegressionRemediationCase:  # noqa: F821
    """Create a MERGED background-service crash RegressionRemediationCase."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase as RRC

    now = _utcnow()
    merged_at = (now - timedelta(minutes=merged_at_offset_minutes)).isoformat()
    snapshot = {
        "service_name": service_name,
        "crash_count": 3,
        "captured_at": merged_at,
    }
    case = RRC(
        project_id=project_id,
        regression_class=f"background_service_crash:{service_name}",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=attempts,
        signal_snapshot_at_spawn=snapshot,
        signal_snapshot_at_merge=snapshot,
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)
    return case


def _make_crash_task(project_id, *, service_name: str) -> Task:
    """Create a recent crash investigation task (simulates lifecycle spawn)."""
    return make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "investigate"}],
        context={
            "spawned_by": "crash_investigation",
            "service_name": service_name,
            "regression_class": f"background_service_crash:{service_name}",
        },
    )


@pytest.mark.asyncio
async def test_crash_case_recovery_closes_when_service_stable(db_session):
    """run_recovery_once closes a MERGED crash case when no recent crash tasks exist."""
    from app.models import RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_crash_case(db_session, project.id, service_name="reaper_service")

    # No recent crash tasks → service is stable → case should be closed.
    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


@pytest.mark.asyncio
async def test_crash_case_rearms_when_still_crashing(db_session):
    """run_recovery_once re-arms a MERGED crash case when a recent crash task exists."""
    from app.models import RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_crash_case(
        db_session, project.id, service_name="orchestrator_worker"
    )

    # Add a recent crash task — service is still crashing.
    crash_task = _make_crash_task(project.id, service_name="orchestrator_worker")
    db_session.add(crash_task)
    await db_session.commit()

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.attempts == 1


@pytest.mark.asyncio
async def test_crash_case_escalates_after_max_attempts(db_session):
    """Three failed remediations escalate the crash case to NEEDS_MANUAL_INTERVENTION."""
    from app.models import RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two prior failed remediations — next re_arm hits the cap of 3.
    case = await _make_crash_case(
        db_session, project.id, service_name="gcp_cleanup_service", attempts=2
    )

    crash_task = _make_crash_task(project.id, service_name="gcp_cleanup_service")
    db_session.add(crash_task)
    await db_session.commit()

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None


@pytest.mark.asyncio
async def test_crash_case_per_service_independent(db_session):
    """Two services crashing independently each get their own case — dedup is per service."""
    from app.models import RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case_reaper = await _make_crash_case(db_session, project.id, service_name="reaper")
    case_worker = await _make_crash_case(
        db_session, project.id, service_name="orchestrator_worker"
    )

    # Only "reaper" has a recent crash task — still crashing.
    crash_task = _make_crash_task(project.id, service_name="reaper")
    db_session.add(crash_task)
    await db_session.commit()

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 2  # Both cases were evaluated and updated.

    await db_session.refresh(case_reaper)
    await db_session.refresh(case_worker)
    # "reaper" still crashing → re-armed to ACTIVE.
    assert case_reaper.status == RegressionCaseStatus.ACTIVE
    # "orchestrator_worker" no recent crashes → closed.
    assert case_worker.status == RegressionCaseStatus.CLOSED


# ---------------------------------------------------------------------------
# Cross-layer regression escalation
# ---------------------------------------------------------------------------


def _seed_anomaly(project_id, fingerprint: str, *, message: str, count: int = 5):
    """Seed a persistent anomaly entry that will trip _evaluate_anomaly_signal.

    Caller is responsible for popping the entry from
    `app.shared_state.persistent_anomalies` after the test.
    """
    import app.shared_state as _ss

    now = _utcnow()
    _ss.persistent_anomalies[str(project_id)] = {
        fingerprint: {
            "occurrence_count": count,
            "last_seen": now.isoformat(),
            "first_seen": (now - timedelta(hours=1)).isoformat(),
            "example_message": message,
            "latest_details": message,
            "severity": "high",
        },
    }


@pytest.mark.asyncio
async def test_cross_layer_one_layer_no_escalation(db_session):
    """A regression class with only ONE layer in the window does NOT escalate."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "PROVISION_FAILURE"
    _seed_anomaly(
        project.id,
        fingerprint,
        message="error while loading shared libraries: libatk-1.0.so.0",
    )
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1

        task = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .first()
        )
        assert task is not None
        ctx = task.context or {}
        assert ctx.get("cross_layer") is not True
        assert ctx.get("escalation") != "cross_layer"
        payload = task.first_payload or {}
        assert "CROSS-LAYER REGRESSION DETECTED" not in (payload.get("prompt") or "")
        assert payload.get("layer_context") in (
            None,
            {},
            {"layers": [], "escalation": "cross_layer"},
        )
    finally:
        import app.shared_state as _ss

        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_cross_layer_two_layers_escalates(db_session):
    """>=2 distinct layers in 48h window → cross-layer investigate w/ layer_context."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.regression_layer_classifier import (
        LAYER_AGENT_VM,
        LAYER_CI_WORKFLOW,
    )

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "PROVISION_FAILURE"
    regression_class = f"anomaly:{fingerprint}"
    now = _utcnow()
    # Prior observation already classified to AGENT_VM.
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
        last_seen_at=now - timedelta(hours=2),
        hit_count=1,
        signal_first_crossed_at=now - timedelta(hours=2),
        signal_first_seen_at=now - timedelta(hours=2),
        symptoms_by_layer={
            LAYER_AGENT_VM: [
                {
                    "fingerprint": "agentvm-fp-1",
                    "observed_at": (now - timedelta(hours=2)).isoformat(),
                    "sample": "error while loading shared libraries: libatk-1.0.so.0",
                }
            ]
        },
    )
    db_session.add(case)
    await db_session.commit()

    # New observation classifies to CI_WORKFLOW. The case has no last_spawn task,
    # so cooldown does not block; the existing-case path updates symptoms.
    _seed_anomaly(
        project.id,
        fingerprint,
        message="no jobs ran for changed files; workflow not triggered",
    )
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1

        task = (
            (
                await db_session.execute(
                    select(Task)
                    .where(Task.project_id == project.id)
                    .order_by(Task.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert task is not None
        ctx = task.context or {}
        assert ctx.get("cross_layer") is True
        assert ctx.get("escalation") == "cross_layer"
        assert ctx.get("escalation_reason") == "cross_layer"
        layers = sorted(ctx.get("cross_layer_layers") or [])
        assert LAYER_AGENT_VM in layers
        assert LAYER_CI_WORKFLOW in layers
        assert ctx.get("regression_class") == regression_class

        payload = task.first_payload or {}
        prompt = payload.get("prompt") or ""
        assert "CROSS-LAYER REGRESSION DETECTED" in prompt
        assert "RECURRENCE META-INVESTIGATION" not in prompt
        assert LAYER_AGENT_VM in prompt
        assert LAYER_CI_WORKFLOW in prompt

        # InvestigatePayload.layer_context populated with per-layer evidence.
        layer_ctx = payload.get("layer_context") or {}
        assert layer_ctx.get("escalation") == "cross_layer"
        layer_entries = layer_ctx.get("layers") or []
        layer_set = {e.get("layer") for e in layer_entries if isinstance(e, dict)}
        assert LAYER_AGENT_VM in layer_set
        assert LAYER_CI_WORKFLOW in layer_set

        # Case-level escalation_reason stamped for operator audit trail.
        await db_session.refresh(case)
        assert case.escalation_reason == "escalated_cross_layer"
    finally:
        import app.shared_state as _ss

        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_cross_layer_mark_merged_records_spawn_context(db_session):
    """After cross-layer spawn + mark_merged, fix-log entry preserves spawn context."""
    from app.models import (
        RegressionCaseStatus,
        RegressionRemediationCase,
    )
    from app.regression_layer_classifier import LAYER_AGENT_VM, LAYER_CI_WORKFLOW

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "PROVISION_FAILURE"
    regression_class = f"anomaly:{fingerprint}"
    now = _utcnow()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
        last_seen_at=now - timedelta(hours=2),
        hit_count=1,
        signal_first_crossed_at=now - timedelta(hours=2),
        signal_first_seen_at=now - timedelta(hours=2),
        symptoms_by_layer={
            LAYER_AGENT_VM: [
                {
                    "fingerprint": "agentvm-fp-1",
                    "observed_at": (now - timedelta(hours=2)).isoformat(),
                    "sample": "error while loading shared libraries",
                }
            ]
        },
    )
    db_session.add(case)
    await db_session.commit()

    case.mark_merged(
        pr_number=555,
        signal_snapshot={"signal_name": "persistent_anomaly"},
        spawn_was_cross_layer=True,
        spawn_layers_enumerated=[LAYER_AGENT_VM, LAYER_CI_WORKFLOW],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    log = case.fingerprint_to_fix_log or []
    assert len(log) == 1
    entry = log[0]
    assert entry["spawn_was_cross_layer"] is True
    assert LAYER_AGENT_VM in entry["spawn_layers_enumerated"]
    assert LAYER_CI_WORKFLOW in entry["spawn_layers_enumerated"]
    assert entry["fix_pr_number"] == 555
    assert "layers_targeted" not in entry  # backfill not yet applied


@pytest.mark.asyncio
async def test_cross_layer_cooldown_still_blocks(db_session):
    """Same regression class re-trips within 6h → cooldown blocks (no double-dispatch)."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "PROVISION_FAILURE"
    _seed_anomaly(
        project.id,
        fingerprint,
        message="error while loading shared libraries: libatk-1.0.so.0",
    )
    try:
        first = await run_once(db_session, project.id)
        assert first == 1

        # Mark first task COMPLETED so it is no longer in-flight; cooldown alone
        # must still block. Re-seed with a DIFFERENT-layer message that would
        # otherwise escalate cross-layer.
        first_task = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .first()
        )
        first_task.status = TaskStatus.COMPLETED
        db_session.add(first_task)
        await db_session.commit()

        _seed_anomaly(
            project.id,
            fingerprint,
            message="no jobs ran for changed files; workflow not triggered",
        )
        second = await run_once(db_session, project.id)
        assert second == 0

        rows = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1  # cooldown blocked the second spawn
    finally:
        import app.shared_state as _ss

        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_cross_layer_counts_as_one_against_system_cap(db_session):
    """A cross-layer escalation occupies exactly one spawn slot, not multiple."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import (
        _REGRESSION_DISPATCHER_SYSTEM_CAP,
    )
    from app.regression_layer_classifier import LAYER_AGENT_VM

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Three persistent anomalies, each with prior AGENT_VM history. The first
    # two will spawn (bucket cross_layer + cross_layer); the third must be
    # capped by _REGRESSION_DISPATCHER_SYSTEM_CAP (=2).
    fingerprints = ["A_PROVISION", "B_PROVISION", "C_PROVISION"]
    for fp in fingerprints:
        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class=f"anomaly:{fp}",
            status=RegressionCaseStatus.ACTIVE,
            signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
            last_seen_at=now - timedelta(hours=2),
            hit_count=1,
            signal_first_crossed_at=now - timedelta(hours=2),
            signal_first_seen_at=now - timedelta(hours=2),
            symptoms_by_layer={
                LAYER_AGENT_VM: [
                    {
                        "fingerprint": f"prior-{fp}",
                        "observed_at": (now - timedelta(hours=1)).isoformat(),
                        "sample": "error while loading shared libraries: libatk.so",
                    }
                ]
            },
        )
        db_session.add(case)
    await db_session.commit()

    import app.shared_state as _ss

    _ss.persistent_anomalies[str(project.id)] = {
        fp: {
            "occurrence_count": 5,
            "last_seen": now.isoformat(),
            "first_seen": (now - timedelta(hours=1)).isoformat(),
            "example_message": "no jobs ran for changed files; workflow not triggered",
            "latest_details": "no jobs ran for changed files; workflow not triggered",
            "severity": "high",
        }
        for fp in fingerprints
    }
    try:
        spawned = await run_once(db_session, project.id)
        # System cap is 2 — even though all three would cross-layer escalate,
        # only the first two get spawn slots.
        assert spawned == _REGRESSION_DISPATCHER_SYSTEM_CAP
        assert _REGRESSION_DISPATCHER_SYSTEM_CAP == 2

        # Filter in Python so the assertion is dialect-agnostic: Postgres jsonb
        # `.as_string() == "true"` works, but SQLite json_extract returns 1 for
        # boolean true and the string compare fails.
        all_tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        cross_layer_tasks = [
            t for t in all_tasks if (t.context or {}).get("cross_layer") is True
        ]
        assert len(cross_layer_tasks) == 2
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_cross_layer_idempotent_repeated_fingerprint(db_session):
    """Repeated identical fingerprint observations don't grow symptoms_by_layer unboundedly."""
    from app.pipeline_regression_dispatcher import (
        _LAYER_SYMPTOM_BOUND,
        _record_layer_observation,
    )
    from app.regression_layer_classifier import LAYER_AGENT_VM

    now = _utcnow()
    symptoms: dict = {}
    # Append same fingerprint 20 times — should stay 1 entry per layer.
    for i in range(20):
        symptoms = _record_layer_observation(
            symptoms,
            [LAYER_AGENT_VM],
            fingerprint="dupe-fp",
            sample=f"sample iter {i}",
            now=now + timedelta(minutes=i),
        )
    assert len(symptoms[LAYER_AGENT_VM]) == 1
    only = symptoms[LAYER_AGENT_VM][0]
    assert only["fingerprint"] == "dupe-fp"
    # observed_at refreshed to latest write.
    assert only["observed_at"] == (now + timedelta(minutes=19)).isoformat()

    # Now append distinct fingerprints past the bound — bound enforced.
    for i in range(_LAYER_SYMPTOM_BOUND + 5):
        symptoms = _record_layer_observation(
            symptoms,
            [LAYER_AGENT_VM],
            fingerprint=f"fresh-fp-{i}",
            sample="x",
            now=now + timedelta(minutes=100 + i),
        )
    # Expect: original "dupe-fp" + bounded set of fresh entries, capped at bound.
    assert len(symptoms[LAYER_AGENT_VM]) <= _LAYER_SYMPTOM_BOUND + 1
    # The dispatcher must have trimmed the oldest fresh entries — newest kept.
    fps = [e["fingerprint"] for e in symptoms[LAYER_AGENT_VM]]
    assert f"fresh-fp-{_LAYER_SYMPTOM_BOUND + 4}" in fps


@pytest.mark.asyncio
async def test_cross_layer_with_recurrence_blends_prompts(db_session):
    """When regression recurs on new layers → both recurrence and cross-layer blocks in prompt."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.regression_layer_classifier import (
        LAYER_AGENT_VM,
        LAYER_CI_WORKFLOW,
    )

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fingerprint = "PROVISION_FAILURE"
    regression_class = f"anomaly:{fingerprint}"
    now = _utcnow()
    merged_at = (now - timedelta(hours=3)).isoformat()

    # Prior spawn with successful remediation.
    prior_task = await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class=regression_class,
        created_at=now - timedelta(hours=7),
        pr_number=4321,
    )

    # Case with symptoms on AGENT_VM and prior fix history.
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
        last_seen_at=now - timedelta(hours=2),
        hit_count=1,
        signal_first_crossed_at=now - timedelta(hours=2),
        signal_first_seen_at=now - timedelta(hours=2),
        attempts=1,
        last_remediation_task_id=prior_task.id,
        last_remediation_pr_number=4321,
        last_remediation_result="failed",
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "agentvm-fp-1",
                "fix_pr_number": 4321,
                "merged_at": merged_at,
            }
        ],
        symptoms_by_layer={
            LAYER_AGENT_VM: [
                {
                    "fingerprint": "agentvm-fp-1",
                    "observed_at": (now - timedelta(hours=2)).isoformat(),
                    "sample": "error while loading shared libraries: libatk-1.0.so.0",
                }
            ]
        },
    )
    db_session.add(case)
    await db_session.commit()

    # New observation on CI_WORKFLOW layer → triggers cross-layer detection.
    _seed_anomaly(
        project.id,
        fingerprint,
        message="no jobs ran for changed files; workflow not triggered",
    )
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1

        task = (
            (
                await db_session.execute(
                    select(Task)
                    .where(Task.project_id == project.id)
                    .order_by(Task.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert task is not None
        ctx = task.context or {}
        assert ctx.get("cross_layer") is True
        assert ctx.get("recurrence_count") == 1

        payload = task.first_payload or {}
        prompt = payload.get("prompt") or ""

        # Both blocks must be present.
        assert "RECURRENCE META-INVESTIGATION" in prompt
        assert "CROSS-LAYER REGRESSION DETECTED" in prompt

        # Verify recurrence appears before cross-layer.
        recurrence_idx = prompt.find("RECURRENCE META-INVESTIGATION")
        cross_layer_idx = prompt.find("CROSS-LAYER REGRESSION DETECTED")
        assert recurrence_idx < cross_layer_idx
        assert recurrence_idx != -1
        assert cross_layer_idx != -1

        # Both layer names should appear in prompt.
        assert LAYER_AGENT_VM in prompt
        assert LAYER_CI_WORKFLOW in prompt

        # Recurrence details should be present.
        assert "4321" in prompt
        assert "Enumerate ALL FIVE canonical layers explicitly:" in prompt
    finally:
        import app.shared_state as _ss

        _ss.persistent_anomalies.pop(str(project.id), None)


# ---------------------------------------------------------------------------
# Watchdog alarm tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staleness_alarm_fires_after_threshold(db_session, monkeypatch):
    """Staleness alarm opens an ERROR alert when last cycle is >300s ago."""
    import time
    import app.pipeline_regression_dispatcher as _disp
    from app.models import Alert, AlertSeverity
    from app.pipeline_regression_dispatcher import (
        _DISPATCHER_STALENESS_THRESHOLD_SECONDS,
        _STALENESS_ALERT_TITLE_PREFIX,
    )

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Inject a stale monotonic timestamp (well past threshold).
    stale_mono = time.monotonic() - (_DISPATCHER_STALENESS_THRESHOLD_SECONDS + 60)
    _disp._last_cycle_monotonic[str(project.id)] = stale_mono

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title.startswith(_STALENESS_ALERT_TITLE_PREFIX)

    # Cleanup
    del _disp._last_cycle_monotonic[str(project.id)]


@pytest.mark.asyncio
async def test_staleness_alarm_auto_resolves(db_session):
    """Staleness alert is dismissed when a fresh cycle is recorded."""
    import time
    import app.pipeline_regression_dispatcher as _disp
    from app.models import Alert
    from app.pipeline_regression_dispatcher import (
        _STALENESS_ALERT_TITLE_PREFIX,
    )

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-create an open staleness alert.
    db_session.add(
        Alert(
            project_id=project.id,
            severity="ERROR",
            title=f"{_STALENESS_ALERT_TITLE_PREFIX} 400s since last cycle",
            message="stale",
        )
    )
    await db_session.commit()

    # Now record a fresh cycle (within threshold).
    _disp._last_cycle_monotonic[str(project.id)] = time.monotonic()

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0

    del _disp._last_cycle_monotonic[str(project.id)]


@pytest.mark.asyncio
async def test_staleness_alarm_no_false_positive_on_first_boot(db_session):
    """No alarm fires when _last_cycle_monotonic has no entry for the project."""
    import app.pipeline_regression_dispatcher as _disp
    from app.models import Alert

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Ensure no entry exists.
    _disp._last_cycle_monotonic.pop(str(project.id), None)

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_zero_spawn_streak_alarm_fires(db_session):
    """Zero-spawn streak alert opens after K consecutive suppressed cycles."""
    from app.models import Alert, AlertSeverity
    from app.pipeline_regression_dispatcher import (
        _DISPATCHER_ZERO_SPAWN_STREAK_K,
        _ZERO_SPAWN_ALERT_TITLE_PREFIX,
    )

    project = make_project(design_doc=_design_doc_with_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert K cycle rows: all have signals_tripped>0 and spawns_emitted==0.
    # Stagger cycle_started_at by 1s so the watchdog's ``order_by(desc)`` read
    # returns them deterministically.
    base = _utcnow()
    for i in range(_DISPATCHER_ZERO_SPAWN_STREAK_K):
        db_session.add(
            DispatcherCycleRecord(
                project_id=project.id,
                cycle_started_at=base + timedelta(seconds=i),
                cycle_finished_at=base + timedelta(seconds=i),
                spawns_emitted=0,
                signals_tripped=1,
                rejections_by_reason={"cooldown": 1},
            )
        )
    await db_session.commit()

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].title.startswith(_ZERO_SPAWN_ALERT_TITLE_PREFIX)


@pytest.mark.asyncio
async def test_zero_spawn_streak_alarm_auto_resolves(db_session):
    """Zero-spawn streak alert is dismissed when a spawn succeeds."""
    from app.models import Alert
    from app.pipeline_regression_dispatcher import (
        _DISPATCHER_ZERO_SPAWN_STREAK_K,
        _ZERO_SPAWN_ALERT_TITLE_PREFIX,
    )

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-create an open zero-spawn-streak alert.
    db_session.add(
        Alert(
            project_id=project.id,
            severity="WARNING",
            title=f"{_ZERO_SPAWN_ALERT_TITLE_PREFIX} 10 consecutive cycles suppressed",
            message="streak",
        )
    )
    await db_session.commit()

    # Insert K-1 suppressed + 1 successful spawn → streak broken.
    base = _utcnow()
    for i in range(_DISPATCHER_ZERO_SPAWN_STREAK_K):
        is_success = i == _DISPATCHER_ZERO_SPAWN_STREAK_K - 1
        db_session.add(
            DispatcherCycleRecord(
                project_id=project.id,
                cycle_started_at=base + timedelta(seconds=i),
                cycle_finished_at=base + timedelta(seconds=i),
                spawns_emitted=1 if is_success else 0,
                signals_tripped=1,
                rejections_by_reason={"cooldown": 0 if is_success else 1},
            )
        )
    await db_session.commit()

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_staleness_alarm_dedup(db_session):
    """Calling watchdog twice while stale creates exactly one open Alert."""
    import time
    import app.pipeline_regression_dispatcher as _disp
    from app.models import Alert
    from app.pipeline_regression_dispatcher import (
        _DISPATCHER_STALENESS_THRESHOLD_SECONDS,
        _STALENESS_ALERT_TITLE_PREFIX,
    )

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_mono = time.monotonic() - (_DISPATCHER_STALENESS_THRESHOLD_SECONDS + 120)
    _disp._last_cycle_monotonic[str(project.id)] = stale_mono

    await run_dispatcher_watchdog_alarms(db_session, [project.id])
    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.title.like(_STALENESS_ALERT_TITLE_PREFIX + "%"),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1

    del _disp._last_cycle_monotonic[str(project.id)]


# ---------------------------------------------------------------------------
# Path-to-layer classification + layers_targeted backfill tests
# ---------------------------------------------------------------------------


def test_classify_layers_from_paths_ci_workflow():
    from app.pipeline_regression_dispatcher import _classify_layers_from_paths

    assert _classify_layers_from_paths([".github/workflows/ci.yml"]) == ["CI_WORKFLOW"]


def test_classify_layers_from_paths_agent_vm():
    from app.pipeline_regression_dispatcher import _classify_layers_from_paths

    assert _classify_layers_from_paths(["infra/packer/agent.pkr.hcl"]) == ["AGENT_VM"]
    assert _classify_layers_from_paths(["setup.sh"]) == ["AGENT_VM"]


def test_classify_layers_from_paths_agent_verb():
    from app.pipeline_regression_dispatcher import _classify_layers_from_paths

    # standard.ts is a non-review handler — must classify as AGENT_VERB
    assert _classify_layers_from_paths(["agent/src/handlers/standard.ts"]) == [
        "AGENT_VERB"
    ]
    assert _classify_layers_from_paths(["agent/src/index.ts"]) == ["AGENT_VERB"]


def test_classify_layers_from_paths_baseline_gate():
    from app.pipeline_regression_dispatcher import _classify_layers_from_paths

    assert _classify_layers_from_paths(["backend/testing/module_size_limits.json"]) == [
        "BASELINE_GATE"
    ]
    assert _classify_layers_from_paths(["admin-ui/eslint.config.js"]) == [
        "BASELINE_GATE"
    ]


def test_classify_layers_from_paths_reviewer_pass():
    from app.pipeline_regression_dispatcher import _classify_layers_from_paths

    assert _classify_layers_from_paths(["backend/app/review_pipeline.py"]) == [
        "REVIEWER_PASS"
    ]
    # review-fix handler must NOT also match AGENT_VERB; reviewer takes priority
    assert _classify_layers_from_paths(["agent/src/handlers/review-fix.ts"]) == [
        "REVIEWER_PASS"
    ]
    assert _classify_layers_from_paths(["agent/src/prompts/self-review.ts"]) == [
        "REVIEWER_PASS"
    ]


def test_classify_layers_from_paths_mixed_returns_sorted_unique():
    from app.pipeline_regression_dispatcher import _classify_layers_from_paths

    layers = _classify_layers_from_paths(
        [
            ".github/workflows/ci.yml",
            "infra/packer/agent.pkr.hcl",
            "agent/src/handlers/standard.ts",
            "backend/testing/module_size_limits.json",
            "backend/app/review_pipeline.py",
            # Duplicate path of the same layer; final list must be deduped
            ".github/workflows/release.yml",
            # Off-canvas path that doesn't match any rule should be ignored
            "README.md",
            "docs/some-note.md",
        ]
    )
    assert layers == [
        "AGENT_VERB",
        "AGENT_VM",
        "BASELINE_GATE",
        "CI_WORKFLOW",
        "REVIEWER_PASS",
    ]


def test_classify_layers_from_paths_no_match_returns_empty():
    from app.pipeline_regression_dispatcher import _classify_layers_from_paths

    assert _classify_layers_from_paths(["README.md"]) == []
    assert _classify_layers_from_paths([]) == []
    # Non-string input is skipped without raising
    assert _classify_layers_from_paths(["", None]) == []  # type: ignore[list-item]


def test_mark_merged_accepts_layers_targeted_directly():
    """Forward-compat: callers that already classified at merge time pass it in."""
    from app.models import RegressionRemediationCase

    case = RegressionRemediationCase(
        project_id=uuid.uuid4(),
        regression_class="sr_catch_rate",
        signal_snapshot_at_spawn={"prior_rate": 0.8, "current_rate": 0.5},
    )
    case.mark_merged(
        4321, {"current_rate": 0.7}, layers_targeted=["AGENT_VM", "CI_WORKFLOW"]
    )
    assert len(case.fingerprint_to_fix_log) == 1
    entry = case.fingerprint_to_fix_log[0]
    assert entry["fix_pr_number"] == 4321
    assert entry["layers_targeted"] == ["AGENT_VM", "CI_WORKFLOW"]


def test_mark_merged_omits_layers_targeted_when_not_provided():
    """Backwards-compat: legacy callers without the kwarg get a key-less entry."""
    from app.models import RegressionRemediationCase

    case = RegressionRemediationCase(
        project_id=uuid.uuid4(),
        regression_class="sr_catch_rate",
        signal_snapshot_at_spawn={"prior_rate": 0.8, "current_rate": 0.5},
    )
    case.mark_merged(99, {})
    assert len(case.fingerprint_to_fix_log) == 1
    entry = case.fingerprint_to_fix_log[0]
    assert entry["fix_pr_number"] == 99
    assert "layers_targeted" not in entry


@pytest.mark.asyncio
async def test_backfill_layers_targeted_populates_missing_entries(
    db_session, monkeypatch
):
    """Backfill scan classifies entries with fix_pr_number but no layers_targeted."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _backfill_layers_targeted_once

    project = make_project(
        github_owner="test-owner",
        github_repo="test-repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 100,
                "merged_at": _utcnow().isoformat(),
            },
            # Entry without fix_pr_number — must be skipped (e.g. recovery row)
            {
                "fingerprint": "sr_catch_rate",
                "recovered_at": _utcnow().isoformat(),
                "post_merge_value": 0.85,
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    async def fake_token(owner, repo):
        assert owner == "test-owner"
        assert repo == "test-repo"
        return "fake-token"

    async def fake_get_pr_files(token, owner, repo, pr_number, project_id=None):
        assert pr_number == 100
        return [
            {"filename": ".github/workflows/ci.yml", "status": "modified"},
            {"filename": "infra/packer/agent.pkr.hcl", "status": "modified"},
        ]

    monkeypatch.setattr("app.github.get_repo_installation_token", fake_token)
    monkeypatch.setattr("app.github.get_pr_files", fake_get_pr_files)

    mutated = await _backfill_layers_targeted_once(db_session, project.id)
    assert mutated == 1

    await db_session.refresh(case)
    log = case.fingerprint_to_fix_log
    fix_entry = next(e for e in log if e.get("fix_pr_number") == 100)
    assert fix_entry["layers_targeted"] == ["AGENT_VM", "CI_WORKFLOW"]
    # The recovery entry without fix_pr_number must remain untouched
    rec_entry = next(e for e in log if e.get("recovered_at"))
    assert "layers_targeted" not in rec_entry


@pytest.mark.asyncio
async def test_backfill_layers_targeted_idempotent(db_session, monkeypatch):
    """A second backfill pass does not re-process entries that already have it."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _backfill_layers_targeted_once

    project = make_project(github_owner="o", github_repo="r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 7,
                "merged_at": _utcnow().isoformat(),
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    call_counter = {"n": 0}

    async def fake_token(owner, repo):
        return "tok"

    async def fake_get_pr_files(token, owner, repo, pr_number, project_id=None):
        call_counter["n"] += 1
        return [{"filename": ".github/workflows/ci.yml", "status": "modified"}]

    monkeypatch.setattr("app.github.get_repo_installation_token", fake_token)
    monkeypatch.setattr("app.github.get_pr_files", fake_get_pr_files)

    first = await _backfill_layers_targeted_once(db_session, project.id)
    assert first == 1
    assert call_counter["n"] == 1

    # Second pass: the entry already has layers_targeted (even if []), so it
    # must be skipped and get_pr_files must NOT be invoked again.
    second = await _backfill_layers_targeted_once(db_session, project.id)
    assert second == 0
    assert call_counter["n"] == 1


@pytest.mark.asyncio
async def test_backfill_skipped_when_no_github_creds(db_session, monkeypatch):
    """Project without github_owner/repo or with no installation token is skipped."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _backfill_layers_targeted_once

    project = make_project(github_owner=None, github_repo=None)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 1,
                "merged_at": _utcnow().isoformat(),
            }
        ],
    )
    db_session.add(case)
    await db_session.commit()

    async def must_not_call_token(owner, repo):
        raise AssertionError("token fetch should not run for project without repo")

    monkeypatch.setattr("app.github.get_repo_installation_token", must_not_call_token)

    mutated = await _backfill_layers_targeted_once(db_session, project.id)
    assert mutated == 0


@pytest.mark.asyncio
async def test_backfill_handles_get_pr_files_error_gracefully(db_session, monkeypatch):
    """Transient GitHub errors are logged and the entry stays untouched."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _backfill_layers_targeted_once

    project = make_project(github_owner="o", github_repo="r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 200,
                "merged_at": _utcnow().isoformat(),
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    async def fake_token(owner, repo):
        return "tok"

    async def boom_get_pr_files(*args, **kwargs):
        raise RuntimeError("github 502")

    monkeypatch.setattr("app.github.get_repo_installation_token", fake_token)
    monkeypatch.setattr("app.github.get_pr_files", boom_get_pr_files)

    # Must not raise — error path swallows + logs.
    mutated = await _backfill_layers_targeted_once(db_session, project.id)
    assert mutated == 0

    await db_session.refresh(case)
    entry = case.fingerprint_to_fix_log[0]
    assert "layers_targeted" not in entry


# ---------------------------------------------------------------------------
# Layer-coverage histogram (admin_digest) — exercised via direct helper call
# so the test stays in this file per cluster scope.
# ---------------------------------------------------------------------------


def test_layer_coverage_histogram_sorts_and_skips_unclassified():
    """Per-class histogram ranks combinations by count desc; skips classes with no
    classified fix-log entries.
    """
    from app.models import RegressionRemediationCase
    from app.routers.admin_digest import _compute_layer_coverage_by_class

    pid = uuid.uuid4()
    # Class A: 3× [AGENT_VM], 2× [AGENT_VM, CI_WORKFLOW] (across two cases)
    case_a1 = RegressionRemediationCase(
        project_id=pid,
        regression_class="baseline_drift",
        fingerprint_to_fix_log=[
            {"fix_pr_number": 1, "layers_targeted": ["AGENT_VM"]},
            {"fix_pr_number": 2, "layers_targeted": ["AGENT_VM"]},
            {"fix_pr_number": 3, "layers_targeted": ["AGENT_VM", "CI_WORKFLOW"]},
        ],
    )
    case_a2 = RegressionRemediationCase(
        project_id=pid,
        regression_class="baseline_drift",
        fingerprint_to_fix_log=[
            {"fix_pr_number": 4, "layers_targeted": ["AGENT_VM"]},
            {"fix_pr_number": 5, "layers_targeted": ["CI_WORKFLOW", "AGENT_VM"]},
            # Entry without layers_targeted → unclassified, excluded
            {"fix_pr_number": 6},
        ],
    )
    # Class B: only unclassified entries → must be omitted from output entirely
    case_b = RegressionRemediationCase(
        project_id=pid,
        regression_class="orphaned",
        fingerprint_to_fix_log=[
            {"fix_pr_number": 7},
        ],
    )

    out = _compute_layer_coverage_by_class([case_a1, case_a2, case_b])
    assert "orphaned" not in out
    assert out["baseline_drift"]["total_fixes_with_layers"] == 5
    combos = out["baseline_drift"]["combinations"]
    # Sorted: [AGENT_VM] (3) first, then [AGENT_VM, CI_WORKFLOW] (2)
    assert combos[0] == {"layers": ["AGENT_VM"], "count": 3}
    assert combos[1] == {"layers": ["AGENT_VM", "CI_WORKFLOW"], "count": 2}


def test_layer_coverage_histogram_empty_layers_targeted_aggregates():
    """An entry with explicit layers_targeted=[] is classified-as-no-layers."""
    from app.models import RegressionRemediationCase
    from app.routers.admin_digest import _compute_layer_coverage_by_class

    pid = uuid.uuid4()
    case = RegressionRemediationCase(
        project_id=pid,
        regression_class="docs_only_fix",
        fingerprint_to_fix_log=[
            {"fix_pr_number": 1, "layers_targeted": []},
            {"fix_pr_number": 2, "layers_targeted": []},
        ],
    )
    out = _compute_layer_coverage_by_class([case])
    assert out["docs_only_fix"]["total_fixes_with_layers"] == 2
    # Empty list aggregates as a no-layer bucket
    assert out["docs_only_fix"]["combinations"] == [{"layers": [], "count": 2}]


def test_regression_case_summary_emits_layer_coverage_field():
    """The regression-case summary response includes per-class layer_coverage."""
    from datetime import datetime as _dt

    from app.models import RegressionRemediationCase
    from app.routers.admin_digest import _compute_regression_case_summary

    pid = uuid.uuid4()
    seen = _dt(2026, 5, 1, 10, 0, 0)
    merged = _dt(2026, 5, 1, 11, 0, 0)
    recovered = _dt(2026, 5, 1, 12, 0, 0)

    case = RegressionRemediationCase(
        project_id=pid,
        regression_class="sr_catch_rate",
        first_seen_at=seen,
        last_seen_at=recovered,
        fingerprint_to_fix_log=[
            {
                "fix_pr_number": 10,
                "merged_at": merged.isoformat(),
                "layers_targeted": ["REVIEWER_PASS"],
            },
            {
                "recovered_at": recovered.isoformat(),
                "post_merge_value": 0.9,
            },
        ],
    )

    summary = _compute_regression_case_summary([case])
    assert "layer_coverage_by_class" in summary
    assert (
        summary["layer_coverage_by_class"]["sr_catch_rate"]["total_fixes_with_layers"]
        == 1
    )
    by_class_entry = next(
        e
        for e in summary["by_regression_class"]
        if e["regression_class"] == "sr_catch_rate"
    )
    assert by_class_entry["layer_coverage"]["combinations"] == [
        {"layers": ["REVIEWER_PASS"], "count": 1}
    ]


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_disallowed_no_spawn(db_session):
    """Disallowed theme at count >= floor → no investigate spawned; disallowed_theme rejection incremented."""
    from app.pipeline_regression_dispatcher import get_dispatcher_cycle_records
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    # "did not run full backend pytest" matches DISALLOWED_BLIND_SPOT_PATTERNS (6 tokens, pattern hits)
    disallowed_text = "did not run full backend pytest"
    for _ in range(7):  # well above the floor of 5
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [disallowed_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    investigate_tasks = [
        t for t in all_tasks if (t.first_payload or {}).get("type") == "investigate"
    ]
    assert investigate_tasks == [], (
        "no investigate task should be spawned for disallowed theme"
    )

    records = await get_dispatcher_cycle_records(db_session, project_id=str(project.id))
    assert records, "cycle record must exist"
    last_record = records[-1]
    assert last_record["rejections_by_reason"].get("disallowed_theme", 0) >= 1


@pytest.mark.asyncio
async def test_sr_blind_spot_theme_allowed_spawns(db_session):
    """Allowed theme at count >= floor → investigate task is spawned normally."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    allowed_text = "unsure whether retry path handles concurrent calls"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [allowed_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    investigate_tasks = [
        t for t in all_tasks if (t.first_payload or {}).get("type") == "investigate"
    ]
    assert len(investigate_tasks) == 1
    ctx = investigate_tasks[0].context or {}
    assert ctx["regression_class"].startswith("sr_blind_spot:theme:")


# ---------------------------------------------------------------------------
# Telemetry-staleness autonomous investigation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_stale_spawns_investigation_for_stale_sr_catch_rate(db_session):
    """sr_catch_rate stale beyond 48h threshold → spawns investigate with telemetry_stale:sr_catch_rate class."""
    from app.models import RegressionRemediationCase
    from app.time_utils import _utcnow

    now = _utcnow()
    stale_ts = (now - timedelta(hours=50)).isoformat()
    project = make_project(
        design_doc={
            "sr_catch_rate_trends": {"signal_last_computed_at": stale_ts},
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned >= 1

    case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class
                    == "telemetry_stale:sr_catch_rate",
                )
            )
        )
        .scalars()
        .first()
    )
    assert case is not None
    assert case.signal_first_seen_at is not None

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    payload = task.first_payload or {}
    assert payload.get("type") == "investigate"
    ctx = task.context or {}
    assert ctx["regression_class"] == "telemetry_stale:sr_catch_rate"
    assert ctx["signal_name"] == "telemetry_stale"
    prompt = payload.get("prompt", "")
    assert "sr_catch_rate" in prompt
    assert "playbook_learning" in prompt


@pytest.mark.asyncio
async def test_telemetry_stale_no_spawn_when_signal_fresh(db_session):
    """sr_catch_rate within 48h freshness window → no investigate spawned."""
    from app.time_utils import _utcnow

    now = _utcnow()
    fresh_ts = (now - timedelta(hours=24)).isoformat()
    project = make_project(
        design_doc={
            "sr_catch_rate_trends": {"signal_last_computed_at": fresh_ts},
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await run_once(db_session, project.id)
    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    investigate_tasks = [
        t
        for t in all_tasks
        if (t.first_payload or {}).get("type") == "investigate"
        and (t.context or {}).get("regression_class", "").startswith("telemetry_stale:")
    ]
    assert len(investigate_tasks) == 0


@pytest.mark.asyncio
async def test_telemetry_stale_dedup_inflight_blocks_second_spawn(db_session):
    """A second run_once with a telemetry_stale case in-flight does not spawn a duplicate."""
    from app.time_utils import _utcnow

    now = _utcnow()
    stale_ts = (now - timedelta(hours=50)).isoformat()
    project = make_project(
        design_doc={
            "sr_catch_rate_trends": {"signal_last_computed_at": stale_ts},
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    first = await run_once(db_session, project.id)
    assert first >= 1

    await run_once(db_session, project.id)
    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    stale_tasks = [
        t
        for t in all_tasks
        if (t.context or {}).get("regression_class") == "telemetry_stale:sr_catch_rate"
    ]
    assert len(stale_tasks) == 1


@pytest.mark.asyncio
async def test_telemetry_stale_recovery_closes_when_signal_refreshed(db_session):
    """Recovery scan closes telemetry_stale case once the signal is within its freshness window."""
    from app.models import RegressionCaseStatus
    from app.time_utils import _utcnow

    now = _utcnow()
    fresh_ts = (now - timedelta(hours=24)).isoformat()
    project = make_project(
        design_doc={
            "sr_catch_rate_trends": {"signal_last_computed_at": fresh_ts},
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _merged_case(project.id, regression_class="telemetry_stale:sr_catch_rate")
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed >= 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED


# ---------------------------------------------------------------------------
# Layer-defense ledger tests
# ---------------------------------------------------------------------------


def test_build_layer_defense_ledger_empty_fix_log_returns_empty_string():
    """Empty fix log produces no block."""
    from app.pipeline_regression_dispatcher import _build_layer_defense_ledger

    assert _build_layer_defense_ledger([]) == ""


def test_build_layer_defense_ledger_groups_by_layer_with_hold_duration():
    """Entries with layers_targeted and both timestamps produce per-layer stats."""
    from app.pipeline_regression_dispatcher import (
        _build_layer_defense_ledger,
        _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
    )
    from app.time_utils import _utcnow

    now = _utcnow()
    merged_at = (now - timedelta(days=10)).isoformat()
    recovered_at = (now - timedelta(days=8)).isoformat()  # held 2 days

    fix_log = [
        {
            "fingerprint": "fp1",
            "fix_pr_number": 100,
            "merged_at": merged_at,
            "recovered_at": recovered_at,
            "layers_targeted": ["BASELINE_GATE"],
        },
        {
            "fingerprint": "fp2",
            "fix_pr_number": 101,
            "merged_at": merged_at,
            "recovered_at": recovered_at,
            "layers_targeted": ["BASELINE_GATE"],
        },
    ]
    block = _build_layer_defense_ledger(fix_log)
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in block
    # All five canonical layers appear
    for layer in (
        "CI_WORKFLOW",
        "AGENT_VM",
        "AGENT_VERB",
        "BASELINE_GATE",
        "REVIEWER_PASS",
    ):
        assert layer in block
    # BASELINE_GATE has 2 defenses
    assert "BASELINE_GATE: 2 defense(s) added" in block
    # Avg hold should be ~2.0d (both held 2d)
    assert "avg hold 2.0d before re-trip" in block
    # Layers with no activity show the "no defenses" phrasing
    assert (
        f"CI_WORKFLOW: no defenses added here in the last {_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS} days"
        in block
    )
    assert (
        f"AGENT_VM: no defenses added here in the last {_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS} days"
        in block
    )


def test_build_layer_defense_ledger_entries_outside_lookback_excluded():
    """Entries with merged_at older than the lookback window are not counted."""
    from app.pipeline_regression_dispatcher import (
        _build_layer_defense_ledger,
        _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
    )
    from app.time_utils import _utcnow

    now = _utcnow()
    # Older than 90 days
    old_merged = (
        now - timedelta(days=_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS + 5)
    ).isoformat()
    old_recovered = (
        now - timedelta(days=_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS + 3)
    ).isoformat()

    fix_log = [
        {
            "fingerprint": "fp_old",
            "merged_at": old_merged,
            "recovered_at": old_recovered,
            "layers_targeted": ["BASELINE_GATE"],
        },
    ]
    block = _build_layer_defense_ledger(fix_log)
    # Block is rendered (non-empty fix_log)
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in block
    # But BASELINE_GATE shows no activity (entry was excluded)
    assert (
        f"BASELINE_GATE: no defenses added here in the last {_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS} days"
        in block
    )
    assert "defense(s) added" not in block


def test_build_layer_defense_ledger_still_holding_and_retripped():
    """Still-holding and re-tripped-within-48h entries are categorised correctly."""
    from app.pipeline_regression_dispatcher import _build_layer_defense_ledger
    from app.time_utils import _utcnow

    now = _utcnow()
    # Still holding (no recovered_at) - 30 days old → held >7d
    merged_still = (now - timedelta(days=30)).isoformat()
    # Re-tripped within 48h
    merged_fast = (now - timedelta(days=5)).isoformat()
    recovered_fast = (now - timedelta(days=4, hours=23)).isoformat()  # ~1h hold < 48h

    fix_log = [
        {
            "fingerprint": "fp_still",
            "merged_at": merged_still,
            "layers_targeted": ["CI_WORKFLOW"],
        },
        {
            "fingerprint": "fp_fast",
            "merged_at": merged_fast,
            "recovered_at": recovered_fast,
            "layers_targeted": ["CI_WORKFLOW"],
        },
    ]
    block = _build_layer_defense_ledger(fix_log)
    assert "CI_WORKFLOW: 2 defense(s) added" in block
    assert "1 still holding" in block
    assert "retripped <48h (fragile)" in block
    # still_holding entry > 7d old → contributes to held_gt_7d
    assert "held >7d" in block


def test_compute_layer_defense_density_empty_returns_zeroed_layers():
    """Empty input yields one zero-row per canonical layer in canonical order."""
    from app.pipeline_regression_dispatcher import (
        _CANONICAL_RECURRENCE_LAYERS,
        compute_layer_defense_density,
    )

    out = compute_layer_defense_density([])
    assert [r["layer"] for r in out] == list(_CANONICAL_RECURRENCE_LAYERS)
    for row in out:
        assert row["fix_count"] == 0
        assert row["last_fix_at"] is None
        assert row["sample_case_ids"] == []


def test_compute_layer_defense_density_canonical_order_with_partial_activity():
    """All five layers appear in canonical order even when only some have entries."""
    from app.pipeline_regression_dispatcher import (
        _CANONICAL_RECURRENCE_LAYERS,
        compute_layer_defense_density,
    )
    from app.time_utils import _utcnow

    now = _utcnow()
    pairs = [
        (
            "case-1",
            {
                "merged_at": (now - timedelta(days=1)).isoformat(),
                "layers_targeted": ["BASELINE_GATE"],
            },
        ),
    ]
    out = compute_layer_defense_density(pairs)
    assert [r["layer"] for r in out] == list(_CANONICAL_RECURRENCE_LAYERS)
    by_layer = {r["layer"]: r for r in out}
    assert by_layer["BASELINE_GATE"]["fix_count"] == 1
    assert by_layer["BASELINE_GATE"]["sample_case_ids"] == ["case-1"]
    assert by_layer["CI_WORKFLOW"]["fix_count"] == 0
    assert by_layer["CI_WORKFLOW"]["last_fix_at"] is None
    assert by_layer["CI_WORKFLOW"]["sample_case_ids"] == []


def test_compute_layer_defense_density_filters_by_lookback():
    """Entries with merged_at outside the lookback are excluded."""
    from app.pipeline_regression_dispatcher import compute_layer_defense_density
    from app.time_utils import _utcnow

    now = _utcnow()
    in_window = (now - timedelta(days=10)).isoformat()
    out_window = (now - timedelta(days=400)).isoformat()
    pairs = [
        (
            "case-recent",
            {
                "merged_at": in_window,
                "layers_targeted": ["CI_WORKFLOW"],
            },
        ),
        (
            "case-old",
            {
                "merged_at": out_window,
                "layers_targeted": ["CI_WORKFLOW"],
            },
        ),
    ]
    out = compute_layer_defense_density(pairs, lookback_days=90)
    by_layer = {r["layer"]: r for r in out}
    assert by_layer["CI_WORKFLOW"]["fix_count"] == 1
    assert by_layer["CI_WORKFLOW"]["sample_case_ids"] == ["case-recent"]
    # last_fix_at corresponds to the in-window entry only.
    assert by_layer["CI_WORKFLOW"]["last_fix_at"] is not None
    assert by_layer["CI_WORKFLOW"]["last_fix_at"].startswith(in_window[:10])


def test_compute_layer_defense_density_sample_case_ids_distinct_recent_first_capped():
    """sample_case_ids is most-recent-first, distinct, capped at 5."""
    from app.pipeline_regression_dispatcher import compute_layer_defense_density
    from app.time_utils import _utcnow

    now = _utcnow()
    pairs: list[tuple[str, dict]] = []
    # 7 distinct case ids, each contributing one BASELINE_GATE entry.
    # case-i has merged_at = now - i days; smaller i = more recent.
    for i in range(1, 8):
        pairs.append(
            (
                f"case-{i}",
                {
                    "merged_at": (now - timedelta(days=i)).isoformat(),
                    "layers_targeted": ["BASELINE_GATE"],
                },
            )
        )
    # Add a duplicate-id entry for case-1 (older) — must not produce a duplicate id.
    pairs.append(
        (
            "case-1",
            {
                "merged_at": (now - timedelta(days=20)).isoformat(),
                "layers_targeted": ["BASELINE_GATE"],
            },
        )
    )
    out = compute_layer_defense_density(pairs)
    by_layer = {r["layer"]: r for r in out}
    samples = by_layer["BASELINE_GATE"]["sample_case_ids"]
    assert len(samples) == 5
    assert samples == ["case-1", "case-2", "case-3", "case-4", "case-5"]
    assert len(set(samples)) == 5  # distinct


def test_compute_layer_defense_density_multi_layer_entry_credits_each_layer():
    """A single entry with two layers_targeted increments each layer's fix_count."""
    from app.pipeline_regression_dispatcher import compute_layer_defense_density
    from app.time_utils import _utcnow

    now = _utcnow()
    pairs = [
        (
            "case-multi",
            {
                "merged_at": (now - timedelta(days=2)).isoformat(),
                "layers_targeted": ["CI_WORKFLOW", "REVIEWER_PASS"],
            },
        ),
    ]
    out = compute_layer_defense_density(pairs)
    by_layer = {r["layer"]: r for r in out}
    assert by_layer["CI_WORKFLOW"]["fix_count"] == 1
    assert by_layer["REVIEWER_PASS"]["fix_count"] == 1
    assert by_layer["CI_WORKFLOW"]["sample_case_ids"] == ["case-multi"]
    assert by_layer["REVIEWER_PASS"]["sample_case_ids"] == ["case-multi"]
    assert by_layer["BASELINE_GATE"]["fix_count"] == 0


def test_format_recurrence_meta_prompt_includes_layer_defense_block():
    """_format_recurrence_meta_prompt appends layer_defense_block when non-empty."""
    from types import SimpleNamespace
    from app.pipeline_regression_dispatcher import _format_recurrence_meta_prompt

    case = SimpleNamespace(
        _recurrence_attempts=[],
        _recurrence_count=1,
        _recurrence_held_text="2 days",
        _recurrence_obs_layers=[],
        fingerprint_to_fix_log=[],
    )
    block = "RECENT DEFENSE ACTIVITY BY LAYER (last 90 days)\n  BASELINE_GATE: 1 defense(s) added"
    result = _format_recurrence_meta_prompt(
        case, "base prompt", layer_defense_block=block
    )
    assert "RECURRENCE META-INVESTIGATION" in result
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in result
    assert "base prompt" in result


def test_format_cross_layer_prompt_includes_layer_defense_block():
    """_format_cross_layer_prompt appends layer_defense_block when non-empty."""
    from app.pipeline_regression_dispatcher import _format_cross_layer_prompt

    block = "RECENT DEFENSE ACTIVITY BY LAYER (last 90 days)\n  AGENT_VM: no defenses added here"
    result = _format_cross_layer_prompt(
        signal_name="fix_loop_divergence",
        regression_class="fix_loop_divergence:fp1",
        layers_evidence=[
            {"layer": "CI_WORKFLOW", "fingerprint": "fp1", "sample": "some error"},
            {"layer": "AGENT_VM", "fingerprint": "fp2", "sample": "other error"},
        ],
        base_lines="base prompt",
        layer_defense_block=block,
    )
    assert "CROSS-LAYER REGRESSION DETECTED" in result
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in result
    assert "base prompt" in result


def test_format_theme_layer_meta_prompt_includes_layer_defense_block():
    """_format_theme_layer_meta_prompt appends layer_defense_block when non-empty."""
    from app.pipeline_regression_dispatcher import _format_theme_layer_meta_prompt

    block = "RECENT DEFENSE ACTIVITY BY LAYER (last 90 days)\n  REVIEWER_PASS: 3 defense(s) added"
    result = _format_theme_layer_meta_prompt("base prompt", layer_defense_block=block)
    assert "META-INVESTIGATION — SR BLIND-SPOT THEME" in result
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in result
    assert "base prompt" in result


@pytest.mark.asyncio
async def test_recurrence_spawn_prompt_has_ledger_block(db_session):
    """Re-armed cases include RECENT DEFENSE ACTIVITY BY LAYER in the spawned prompt."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    merged_at = (now - timedelta(hours=3)).isoformat()
    recovered_at = (now - timedelta(hours=2, minutes=30)).isoformat()
    prior_task = await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        created_at=now - timedelta(hours=7),
        pr_number=9900,
    )
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=0,
        last_remediation_task_id=prior_task.id,
        last_remediation_pr_number=9900,
        last_remediation_result="merged",
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
            "captured_at": merged_at,
        },
        signal_snapshot_at_merge={"captured_at": merged_at},
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 9900,
                "merged_at": merged_at,
                "recovered_at": recovered_at,
                "layers_targeted": ["BASELINE_GATE"],
            }
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await run_recovery_once(db_session, project.id)
    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "RECURRENCE META-INVESTIGATION" in prompt
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in prompt
    assert "BASELINE_GATE" in prompt


@pytest.mark.asyncio
async def test_theme_spawn_prompt_has_ledger_block(db_session):
    """SR blind-spot theme spawns include RECENT DEFENSE ACTIVITY BY LAYER in the prompt."""
    from app.pipeline_regression_dispatcher import _SR_BLIND_SPOT_THEME_FLOOR
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.time_utils import _utcnow as _ts_now

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    theme_text = "missing null guard in processing loop"
    for _ in range(_SR_BLIND_SPOT_THEME_FLOOR):
        sro = {
            "verdict": "pass",
            "check_results": {"correctness": {"passed": True}},
            "blind_spots": [theme_text],
            "reviewer_findings": [],
        }
        db_session.add(
            make_task(
                project_id=project.id,
                status=TaskStatus.COMPLETED,
                payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
                context={"_self_review_outcome": sro},
                created_at=now - timedelta(days=3),
            )
        )

    # Seed a resolved case with a layers_targeted fix-log entry so the ledger has content
    existing_case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="fix_loop_divergence:some_fp",
        status=RegressionCaseStatus.CLOSED,
        hit_count=1,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "some_fp",
                "fix_pr_number": 5555,
                "merged_at": (now - timedelta(days=5)).isoformat(),
                "recovered_at": (now - timedelta(days=3)).isoformat(),
                "layers_targeted": ["AGENT_VERB"],
            }
        ],
    )
    db_session.add(existing_case)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = _get_investigate_task(all_tasks)
    assert task is not None
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "META-INVESTIGATION" in prompt
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in prompt
    assert "AGENT_VERB" in prompt


@pytest.mark.asyncio
async def test_ledger_computed_once_per_run_once(db_session, monkeypatch):
    """_build_layer_defense_ledger is called at most once per project per run_once call."""
    import app.pipeline_regression_dispatcher as mod

    # Two SR category regressions so run_once calls _maybe_spawn twice for the same project.
    project = make_project(
        design_doc={
            "sr_catch_rate_trends": {
                "categories": {
                    "correctness": {
                        "current_hit_rate": 0.50,
                        "previous_hit_rate": 0.80,
                        "current_human_count": 12,
                        "trend": "decreasing",
                    },
                    "error-handling": {
                        "current_hit_rate": 0.45,
                        "previous_hit_rate": 0.85,
                        "current_human_count": 15,
                        "trend": "decreasing",
                    },
                },
            },
        }
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    call_count = [0]
    original_fn = mod._build_layer_defense_ledger

    def counting_wrapper(all_fix_logs, **kwargs):
        call_count[0] += 1
        return original_fn(all_fix_logs, **kwargs)

    monkeypatch.setattr(mod, "_build_layer_defense_ledger", counting_wrapper)

    spawned = await run_once(db_session, project.id)
    # At least one spawn happened (may be 1 or 2 depending on budget/cooldowns)
    assert spawned >= 1
    # The ledger was computed only once for this project despite multiple _maybe_spawn calls
    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Fast-track failed-remediation cooldown bypass (cluster 1/2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_re_arm_default_sets_fast_track_next_spawn():
    """re_arm() with default args (failure-driven path) flips fast_track_next_spawn."""
    from app.models import (
        RegressionCaseStatus,
        RegressionRemediationCase,
    )

    case = RegressionRemediationCase(
        project_id=uuid.uuid4(),
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        attempts=0,
        hit_count=1,
    )
    case.re_arm()
    assert case.attempts == 1
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.fast_track_next_spawn is True


@pytest.mark.asyncio
async def test_re_arm_fast_track_false_does_not_flip_flag():
    """re_arm(fast_track=False) (recovery-scan path) leaves the flag clear."""
    from app.models import (
        RegressionCaseStatus,
        RegressionRemediationCase,
    )

    case = RegressionRemediationCase(
        project_id=uuid.uuid4(),
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        attempts=0,
        hit_count=1,
    )
    case.re_arm(fast_track=False)
    assert case.attempts == 1
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.fast_track_next_spawn is False


@pytest.mark.asyncio
async def test_re_arm_at_max_attempts_does_not_set_fast_track():
    """Escalation to NEEDS_MANUAL_INTERVENTION must not arm a fast-track spawn."""
    from app.models import (
        ESCALATION_REASON_ATTEMPTS_EXHAUSTED,
        RegressionCaseStatus,
        RegressionRemediationCase,
    )

    case = RegressionRemediationCase(
        project_id=uuid.uuid4(),
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        attempts=2,
        hit_count=1,
    )
    case.re_arm()  # attempts -> 3, escalates
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason == ESCALATION_REASON_ATTEMPTS_EXHAUSTED
    assert case.fast_track_next_spawn is False


@pytest.mark.asyncio
async def test_fast_track_bypasses_cooldown_and_clears_flag(db_session):
    """fast_track_next_spawn=True with attempts>0 bypasses the 6h cooldown."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.50
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Prior dispatcher-spawned task was 1 minute ago (well inside 6h window).
    prior_spawn = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(minutes=1),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(prior_spawn)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.ACTIVE,
        attempts=1,
        hit_count=1,
        fast_track_next_spawn=True,
        last_failure_class="REVIEW_REJECTED",
        last_failure_summary="Reviewer requested broader test coverage on edge cases.",
        last_remediation_pr_number=4123,
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    await db_session.refresh(case)
    # Flag must be cleared (one-shot bypass).
    assert case.fast_track_next_spawn is False

    # The new investigate task carries the prior failure context block.
    new_task = (
        (
            await db_session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.QUEUED,
                )
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert new_task is not None
    prompt = (new_task.first_payload or {}).get("prompt", "")
    assert "PRIOR REMEDIATION FAILURE CONTEXT" in prompt
    assert "REVIEW_REJECTED" in prompt
    assert "#4123" in prompt
    assert "broader test coverage" in prompt


@pytest.mark.asyncio
async def test_fast_track_does_not_bypass_when_attempts_zero(db_session):
    """fast_track_next_spawn=True but attempts=0 does NOT bypass cooldown.

    Defends against a race where the flag is set on an unrelated path: the
    bypass requires attempts > 0 so it can only fire after at least one
    failed remediation cycle.
    """
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.50
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_spawn = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(minutes=1),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(prior_spawn)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.ACTIVE,
        attempts=0,
        hit_count=1,
        fast_track_next_spawn=True,
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
    )
    db_session.add(case)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_fast_track_dedup_still_blocks(db_session):
    """An in-flight investigation blocks fast-track spawns just like normal spawns."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.50
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # In-flight investigation already running for this regression_class.
    in_flight = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        created_at=_utcnow() - timedelta(minutes=1),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
        payloads=[{"type": "investigate", "prompt": "prior", "base_branch": "main"}],
    )
    db_session.add(in_flight)
    await db_session.commit()
    await db_session.refresh(in_flight)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.ACTIVE,
        attempts=1,
        hit_count=1,
        fast_track_next_spawn=True,
        last_remediation_task_id=in_flight.id,
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    # Flag remains armed: dedup blocked the bypass before it could be honored.
    await db_session.refresh(case)
    assert case.fast_track_next_spawn is True


@pytest.mark.asyncio
async def test_fast_track_consumes_system_cap_slot(db_session, monkeypatch):
    """A fast-track spawn occupies one system-cap slot, not unlimited.

    Cap=1, two regression classes tripped (one fast-tracked inside cooldown,
    one fresh). Only the first one to be evaluated gets a slot.
    """
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app import pipeline_regression_dispatcher as mod

    monkeypatch.setattr(mod, "_REGRESSION_DISPATCHER_SYSTEM_CAP", 1)

    project = make_project(
        design_doc={
            "sr_catch_rate_trends": {
                "categories": {
                    "correctness": {
                        "current_hit_rate": 0.50,
                        "previous_hit_rate": 0.80,
                        "current_human_count": 12,
                        "trend": "decreasing",
                    },
                },
            },
            # Pipeline success-rate regression (separate signal class).
            "pipeline_success_rate_trend": {
                "previous_success_rate": 0.90,
                "current_success_rate": 0.50,
                "current_total": 20,
                "previous_total": 20,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # sr_catch_rate already has a recent spawn and a fast-tracked case.
    prior_spawn = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(minutes=1),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(prior_spawn)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.ACTIVE,
        attempts=1,
        hit_count=1,
        fast_track_next_spawn=True,
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
    )
    db_session.add(case)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    # System cap is 1 — only one of the two tripped signals gets a slot.
    assert spawned == 1


@pytest.mark.asyncio
async def test_recovery_rearm_does_not_set_fast_track(db_session):
    """Recovery-scan path's re_arm(fast_track=False) does not arm the bypass."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.50
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # MERGED case whose signal re-tripped but cooldown has elapsed (>6h prior spawn).
    prior_spawn = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
    )
    db_session.add(prior_spawn)

    merged_at = (_utcnow() - timedelta(hours=10)).isoformat()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=0,
        signal_snapshot_at_merge={"captured_at": merged_at},
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
        fingerprint_to_fix_log=[],
    )
    db_session.add(case)
    await db_session.commit()
    await db_session.refresh(case)

    changed = await run_recovery_once(db_session, project.id)
    assert changed == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE
    assert case.attempts == 1
    # Recovery-scan re-arm must NOT set the fast-track flag.
    assert case.fast_track_next_spawn is False


@pytest.mark.asyncio
async def test_fast_track_falls_back_to_task_status_message(db_session):
    """When last_failure_summary is null, prompt falls back to prior task's latest_status_message."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.50
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=_utcnow() - timedelta(minutes=1),
        context={
            "spawned_by": "pipeline_regression_dispatcher",
            "regression_class": "sr_catch_rate",
        },
        latest_status_message="MERGE_BLOCKED: PR conflicts with main on backend/app/x.py",
    )
    db_session.add(prior_task)
    await db_session.commit()
    await db_session.refresh(prior_task)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.ACTIVE,
        attempts=1,
        hit_count=1,
        fast_track_next_spawn=True,
        last_failure_class="MERGE_BLOCKED",
        last_failure_summary=None,
        last_remediation_task_id=prior_task.id,
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
    )
    db_session.add(case)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    new_task = (
        (
            await db_session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status == TaskStatus.QUEUED,
                )
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert new_task is not None
    prompt = (new_task.first_payload or {}).get("prompt", "")
    assert "PRIOR REMEDIATION FAILURE CONTEXT" in prompt
    assert "MERGE_BLOCKED" in prompt
    assert "conflicts with main" in prompt


# ---------------------------------------------------------------------------
# sr_false_positive signal class
# ---------------------------------------------------------------------------


def _design_doc_with_fp_regression(
    *,
    category: str = "correctness",
    current_fp_rate: float = 0.50,
    prev_fp_rate: float = 0.45,
    human_count: int = 10,
) -> dict:
    """Synthesize a sr_catch_rate_trends snapshot that trips the sr_false_positive signal."""
    return {
        "sr_catch_rate_trends": {
            "categories": {
                category: {
                    "current_hit_rate": 0.80,
                    "previous_hit_rate": 0.78,
                    "current_human_count": human_count,
                    "trend": "stable",
                    "false_positive_rate": current_fp_rate,
                    "previous_fp_rate": prev_fp_rate,
                    "fp_trend": "increasing",
                    "precision": round(1 - current_fp_rate, 3),
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_sr_false_positive_signal_trips_when_both_windows_above_floor(db_session):
    """sr_false_positive signal spawns an investigation when FP rate exceeds floor in both windows."""
    project = make_project(
        design_doc=_design_doc_with_fp_regression(
            category="correctness",
            current_fp_rate=0.50,
            prev_fp_rate=0.45,
            human_count=10,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    payload = task.first_payload or {}
    assert payload.get("type") == "investigate"
    prompt = payload.get("prompt", "")
    assert "SR over-paranoia detected" in prompt
    assert "correctness" in prompt
    assert "50.0%" in prompt
    assert (
        "NOT an application-code change" in prompt
        or "prompt/criterion change" in prompt
    )

    ctx = task.context or {}
    assert ctx["spawned_by"] == "pipeline_regression_dispatcher"
    assert ctx["regression_class"] == "sr_false_positive"
    assert ctx["signal_name"] == "sr_false_positive"


@pytest.mark.asyncio
async def test_sr_false_positive_signal_does_not_trip_when_only_one_window_above(
    db_session,
):
    """sr_false_positive does NOT spawn when only the current window exceeds the floor."""
    project = make_project(
        design_doc=_design_doc_with_fp_regression(
            category="correctness",
            current_fp_rate=0.50,
            prev_fp_rate=0.20,  # prior window below floor
            human_count=10,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_sr_false_positive_signal_does_not_trip_below_sample_floor(db_session):
    """sr_false_positive does NOT spawn when sample size is below the minimum."""
    project = make_project(
        design_doc=_design_doc_with_fp_regression(
            category="correctness",
            current_fp_rate=0.50,
            prev_fp_rate=0.45,
            human_count=3,  # below _SR_FP_MIN_SAMPLE=5
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


# ---------------------------------------------------------------------------
# Cross-layer backfill-lag watchdog tests
# ---------------------------------------------------------------------------


def _make_cross_layer_fix_log_case(
    project_id,
    *,
    hours_ago: int,
    has_layers_targeted: bool = False,
    spawn_was_cross_layer: bool = True,
):
    from app.models import RegressionRemediationCase as RRC, RegressionCaseStatus

    entry: dict = {
        "fingerprint": "test_fp",
        "fix_pr_number": 999,
        "merged_at": (_utcnow() - timedelta(hours=hours_ago)).isoformat(),
        "spawn_was_cross_layer": spawn_was_cross_layer,
        "spawn_layers_enumerated": ["CI_WORKFLOW", "AGENT_VM"],
    }
    if has_layers_targeted:
        entry["layers_targeted"] = ["CI_WORKFLOW", "AGENT_VM"]
    return RRC(
        project_id=project_id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.ACTIVE,
        hit_count=1,
        fingerprint_to_fix_log=[entry],
    )


@pytest.mark.asyncio
async def test_cross_layer_backfill_lag_alert_fires_when_stale_entries_exist(
    db_session,
):
    """Cross-layer entry older than threshold with no layers_targeted → INFO alert opened."""
    from app.models import Alert

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_cross_layer_fix_log_case(
        project.id, hours_ago=48, has_layers_targeted=False
    )
    db_session.add(case)
    await db_session.commit()

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    backfill_alerts = [
        a for a in alerts if "Cross-layer backfill lag:" in (a.title or "")
    ]
    assert len(backfill_alerts) == 1
    assert backfill_alerts[0].dismissed_at is None
    assert backfill_alerts[0].severity == "INFO"
    assert "1 cross-layer fix-log entries unbackfilled" in backfill_alerts[0].message


@pytest.mark.asyncio
async def test_cross_layer_backfill_lag_alert_resolves_when_entries_backfilled(
    db_session,
):
    """All cross-layer entries have layers_targeted → existing alert is dismissed."""
    from app.models import Alert, AlertSeverity

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-create an open alert
    alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.INFO,
        title="Cross-layer backfill lag: 1 entries unbackfilled",
        message="old message",
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)

    # All entries are now backfilled
    case = _make_cross_layer_fix_log_case(
        project.id, hours_ago=48, has_layers_targeted=True
    )
    db_session.add(case)
    await db_session.commit()

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    await db_session.refresh(alert)
    assert alert.dismissed_at is not None


@pytest.mark.asyncio
async def test_cross_layer_backfill_lag_not_triggered_for_recent_entries(db_session):
    """Entry missing layers_targeted but merged within threshold → no alert."""
    from app.models import Alert

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Only 2 hours old — below the 24h default threshold
    case = _make_cross_layer_fix_log_case(
        project.id, hours_ago=2, has_layers_targeted=False
    )
    db_session.add(case)
    await db_session.commit()

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    backfill_alerts = [
        a for a in alerts if "Cross-layer backfill lag:" in (a.title or "")
    ]
    assert len(backfill_alerts) == 0


@pytest.mark.asyncio
async def test_regression_mtta_data_eta_recovery_per_case(db_session):
    """Multi-MERGED-case ETA fields are computed per-case, not from a leaked loop var.

    Two MERGED cases (in different projects but the same regression_class, so
    they roll up into the same class bucket) with different merged_at times
    must yield distinct eta_to_recovery_sec values keyed off each case's own
    elapsed-since-merge. The unique (project_id, regression_class) constraint
    forces using two projects to exercise multi-case-per-class iteration.
    """
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    rclass = "sr_catch_rate:correctness:dir-a"
    project_a = make_project(design_doc={})
    project_b = make_project(design_doc={})
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    now = _utcnow()
    # Each MERGED case carries its own merged_at in fingerprint_to_fix_log.
    # Without a paired recovered_at these entries don't contribute to p50_mtor
    # — we add CLOSED siblings (one per project) to seed mtor samples.
    case_a_merged_at = (now - timedelta(seconds=120)).isoformat()
    case_a = RegressionRemediationCase(
        project_id=project_a.id,
        regression_class=rclass,
        status=RegressionCaseStatus.MERGED,
        first_seen_at=now - timedelta(minutes=30),
        signal_first_crossed_at=now - timedelta(minutes=30),
        investigate_spawned_at=now - timedelta(minutes=29),
        fingerprint_to_fix_log=[{"merged_at": case_a_merged_at}],
    )
    case_b_merged_at = (now - timedelta(seconds=900)).isoformat()
    case_b = RegressionRemediationCase(
        project_id=project_b.id,
        regression_class=rclass,
        status=RegressionCaseStatus.MERGED,
        first_seen_at=now - timedelta(minutes=45),
        signal_first_crossed_at=now - timedelta(minutes=45),
        investigate_spawned_at=now - timedelta(minutes=44),
        fingerprint_to_fix_log=[{"merged_at": case_b_merged_at}],
    )
    db_session.add(case_a)
    db_session.add(case_b)
    await db_session.commit()
    await db_session.refresh(case_a)
    await db_session.refresh(case_b)

    # All-projects query exercises multi-case-per-class iteration.
    data = await get_regression_mtta_data(db_session)

    rows = {r["regression_class"]: r for r in data["classes"]}
    row = rows[rclass]
    actives = {a["case_id"]: a for a in row["active_cases"]}
    a_info = actives[str(case_a.id)]
    b_info = actives[str(case_b.id)]

    # elapsed_since_merge_sec must reflect each case's own merged_at.
    assert a_info["elapsed_since_merge_sec"] is not None
    assert b_info["elapsed_since_merge_sec"] is not None
    assert b_info["elapsed_since_merge_sec"] > a_info["elapsed_since_merge_sec"]

    # recovery_check_in_sec is also per-case (case_b's cooldown may already
    # be exhausted depending on _REGRESSION_RECOVERY_COOLDOWN, but the field
    # for case_a — fresh merge — must be populated).
    assert a_info["recovery_check_in_sec"] is not None
    assert a_info["recovery_check_in_sec"] > 0

    # With no mtor samples (no recovered_at entries), p50_mtor is None and
    # both ETAs are None — but the leak would still surface if elapsed values
    # were shared. Force a recoverable-class bucket to populate p50_mtor by
    # appending a recovered_at-bearing entry on case_b's log (this case is
    # already MERGED so it acts as historical recovery data for the class).
    case_b.fingerprint_to_fix_log = [
        {"merged_at": case_b_merged_at},
        {
            "merged_at": (now - timedelta(hours=3)).isoformat(),
            "recovered_at": (
                now - timedelta(hours=3) + timedelta(seconds=600)
            ).isoformat(),
        },
    ]
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    flag_modified(case_b, "fingerprint_to_fix_log")
    await db_session.commit()

    data = await get_regression_mtta_data(db_session)
    row = next(r for r in data["classes"] if r["regression_class"] == rclass)
    actives = {a["case_id"]: a for a in row["active_cases"]}
    a_info = actives[str(case_a.id)]
    b_info = actives[str(case_b.id)]
    p50_mtor = row["stages"]["merge_to_recovery"]["p50_sec"]
    assert p50_mtor == 600

    # ETA must differ per case (regression test for the leaked-loop-var bug
    # that previously gave all active cases the last case's rec_check_in).
    assert a_info["eta_to_recovery_sec"] != b_info["eta_to_recovery_sec"]
    assert a_info["eta_to_recovery_sec"] == int(
        p50_mtor - a_info["elapsed_since_merge_sec"]
    )
    assert b_info["eta_to_recovery_sec"] == int(
        p50_mtor - b_info["elapsed_since_merge_sec"]
    )


@pytest.mark.asyncio
async def test_cross_layer_backfill_lag_ignores_non_cross_layer_entries(db_session):
    """Entries without spawn_was_cross_layer=True are not counted, no alert fires."""
    from app.models import Alert

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # spawn_was_cross_layer=False — should be skipped regardless of age / backfill state
    case = _make_cross_layer_fix_log_case(
        project.id, hours_ago=72, has_layers_targeted=False, spawn_was_cross_layer=False
    )
    db_session.add(case)
    await db_session.commit()

    await run_dispatcher_watchdog_alarms(db_session, [project.id])

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    backfill_alerts = [
        a for a in alerts if "Cross-layer backfill lag:" in (a.title or "")
    ]
    assert len(backfill_alerts) == 0


# ---------------------------------------------------------------------------
# _compute_layer_fragility_stats unit tests
# ---------------------------------------------------------------------------


def test_compute_layer_fragility_stats_empty_returns_zeroed():
    """Empty input yields zeroed stats for all canonical layers."""
    from app.pipeline_regression_dispatcher import (
        _CANONICAL_RECURRENCE_LAYERS,
        _compute_layer_fragility_stats,
    )

    stats = _compute_layer_fragility_stats([])
    assert set(stats.keys()) == set(_CANONICAL_RECURRENCE_LAYERS)
    for s in stats.values():
        assert s["fix_count"] == 0
        assert s["avg_hold_days"] is None
        assert s["still_holding_count"] == 0
        assert s["held_gt_7d_count"] == 0
        assert s["retripped_within_48h_count"] == 0


def test_compute_layer_fragility_stats_avg_hold_and_retrips():
    """Entries with recovered_at produce correct avg_hold_days and retrip counts."""
    from app.pipeline_regression_dispatcher import _compute_layer_fragility_stats

    now = _utcnow()
    entries = [
        {
            "merged_at": (now - timedelta(days=6)).isoformat(),
            "recovered_at": (
                now - timedelta(days=5)
            ).isoformat(),  # hold=1d <2d → retrip
            "layers_targeted": ["CI_WORKFLOW"],
        },
        {
            "merged_at": (now - timedelta(days=20)).isoformat(),
            "recovered_at": (now - timedelta(days=10)).isoformat(),  # hold=10d >7d
            "layers_targeted": ["CI_WORKFLOW"],
        },
    ]
    stats = _compute_layer_fragility_stats(entries)
    s = stats["CI_WORKFLOW"]
    assert s["fix_count"] == 2
    assert s["retripped_within_48h_count"] == 1
    assert s["held_gt_7d_count"] == 1
    assert s["avg_hold_days"] is not None
    assert abs(s["avg_hold_days"] - 5.5) < 0.1  # avg of 1d and 10d


def test_compute_layer_fragility_stats_all_still_holding():
    """No recovered_at entries → avg_hold_days is None, retrips=0."""
    from app.pipeline_regression_dispatcher import _compute_layer_fragility_stats

    now = _utcnow()
    entries = [
        {
            "merged_at": (now - timedelta(days=5)).isoformat(),
            "layers_targeted": ["BASELINE_GATE"],
        },
        {
            "merged_at": (now - timedelta(days=15)).isoformat(),  # >7d old → held_gt_7d
            "layers_targeted": ["BASELINE_GATE"],
        },
    ]
    stats = _compute_layer_fragility_stats(entries)
    s = stats["BASELINE_GATE"]
    assert s["fix_count"] == 2
    assert s["avg_hold_days"] is None
    assert s["still_holding_count"] == 2
    assert s["retripped_within_48h_count"] == 0
    assert s["held_gt_7d_count"] == 1  # only the 15d-old entry


def test_compute_layer_fragility_stats_durable_long_hold_not_fragile():
    """Long-hold re-tripped entry (>7d) does not trip retripped_within_48h_count."""
    from app.pipeline_regression_dispatcher import _compute_layer_fragility_stats

    now = _utcnow()
    entries = [
        {
            "merged_at": (now - timedelta(days=30)).isoformat(),
            "recovered_at": (
                now - timedelta(days=20)
            ).isoformat(),  # hold=10d >7d, not <2d
            "layers_targeted": ["REVIEWER_PASS"],
        },
    ]
    stats = _compute_layer_fragility_stats(entries)
    s = stats["REVIEWER_PASS"]
    assert s["retripped_within_48h_count"] == 0
    assert s["held_gt_7d_count"] == 1
    assert s["avg_hold_days"] is not None
    assert s["avg_hold_days"] > 7.0


def test_build_layer_defense_ledger_unchanged_after_refactor():
    """String output after helper refactor matches pre-refactor fixture."""
    from app.pipeline_regression_dispatcher import (
        _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
        _build_layer_defense_ledger,
    )

    now = _utcnow()
    merged_at = (now - timedelta(days=10)).isoformat()
    recovered_at = (now - timedelta(days=8)).isoformat()  # hold=2d
    fix_log = [
        {
            "fingerprint": "fp1",
            "merged_at": merged_at,
            "recovered_at": recovered_at,
            "layers_targeted": ["BASELINE_GATE"],
        }
    ]
    block = _build_layer_defense_ledger(fix_log)
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in block
    assert "BASELINE_GATE: 1 defense(s) added" in block
    assert "avg hold 2.0d before re-trip" in block
    assert (
        f"CI_WORKFLOW: no defenses added here in the last {_LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS} days"
        in block
    )


# ---------------------------------------------------------------------------
# _format_layer_concentration_prompt unit tests
# ---------------------------------------------------------------------------


def test_format_layer_concentration_prompt_content():
    """Prompt includes header, case IDs with retention info, structural questions, footer."""
    from app.pipeline_regression_dispatcher import _format_layer_concentration_prompt

    now = _utcnow()
    case_id = "case-abc"
    merged = (now - timedelta(days=3)).isoformat()
    recovered = (now - timedelta(days=2)).isoformat()
    all_pairs: list[tuple[str, dict]] = [
        (
            case_id,
            {
                "merged_at": merged,
                "recovered_at": recovered,
                "layers_targeted": ["CI_WORKFLOW"],
            },
        )
    ]
    ledger = "\nRECENT DEFENSE ACTIVITY BY LAYER (last 90 days)\n  CI_WORKFLOW: 1 defense(s) added"

    prompt = _format_layer_concentration_prompt(
        layer="CI_WORKFLOW",
        sample_case_ids=[case_id],
        all_fix_logs_with_case_ids=all_pairs,
        layer_defense_block=ledger,
    )
    assert (
        "LAYER-CONCENTRATION META-INVESTIGATION (layer_concentration:CI_WORKFLOW)"
        in prompt
    )
    assert "recurrent failure mode" in prompt
    assert case_id in prompt
    assert "re-tripped" in prompt
    assert "STRUCTURAL QUESTIONS" in prompt
    assert ".github/workflows/" in prompt
    assert "RECENT DEFENSE ACTIVITY BY LAYER" in prompt


# ---------------------------------------------------------------------------
# _evaluate_layer_concentration_signal integration tests
# ---------------------------------------------------------------------------


def _make_lc_fix_log_entry(
    now,
    merged_days_ago: float,
    recovered_days_ago: float | None,
    layer: str,
) -> dict:
    merged = (now - timedelta(days=merged_days_ago)).isoformat()
    entry: dict = {"merged_at": merged, "layers_targeted": [layer]}
    if recovered_days_ago is not None:
        entry["recovered_at"] = (now - timedelta(days=recovered_days_ago)).isoformat()
    return entry


@pytest.mark.asyncio
async def test_layer_concentration_fires_when_thresholds_met(db_session):
    """3 distinct cases with retripped <48h entries → spawns layer_concentration:CI_WORKFLOW."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    for i in range(3):
        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class=f"anomaly:fp_lc_{i}",
            status=RegressionCaseStatus.ACTIVE,
            signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
            last_seen_at=now - timedelta(hours=i + 1),
            hit_count=1,
            signal_first_crossed_at=now - timedelta(hours=i + 1),
            signal_first_seen_at=now - timedelta(hours=i + 1),
            fingerprint_to_fix_log=[
                _make_lc_fix_log_entry(
                    now,
                    merged_days_ago=5 + i,
                    recovered_days_ago=4 + i,
                    layer="CI_WORKFLOW",
                )
            ],
        )
        db_session.add(case)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned >= 1

    tasks = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    lc_tasks = [
        t
        for t in tasks
        if (t.context or {})
        .get("regression_class", "")
        .startswith("layer_concentration:")
    ]
    assert len(lc_tasks) >= 1

    task = lc_tasks[0]
    ctx = task.context or {}
    assert ctx["regression_class"] == "layer_concentration:CI_WORKFLOW"
    assert ctx["signal_name"] == "layer_concentration"

    payload = task.first_payload or {}
    prompt = payload.get("prompt") or ""
    assert (
        "LAYER-CONCENTRATION META-INVESTIGATION (layer_concentration:CI_WORKFLOW)"
        in prompt
    )
    assert "STRUCTURAL QUESTIONS" in prompt

    # Case-level escalation_reason stamped for operator audit trail.
    lc_case = (
        (
            await db_session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project.id,
                    RegressionRemediationCase.regression_class
                    == "layer_concentration:CI_WORKFLOW",
                )
            )
        )
        .scalars()
        .first()
    )
    assert lc_case is not None
    assert lc_case.escalation_reason == "escalated_layer_concentration"


@pytest.mark.asyncio
async def test_layer_concentration_no_escalation_below_case_threshold(db_session):
    """Only 2 distinct cases → does NOT spawn even when fragile."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    for i in range(2):
        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class=f"anomaly:fp_lc2_{i}",
            status=RegressionCaseStatus.ACTIVE,
            signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
            last_seen_at=now - timedelta(hours=i + 1),
            hit_count=1,
            signal_first_crossed_at=now - timedelta(hours=i + 1),
            signal_first_seen_at=now - timedelta(hours=i + 1),
            fingerprint_to_fix_log=[
                _make_lc_fix_log_entry(
                    now, merged_days_ago=5, recovered_days_ago=4, layer="CI_WORKFLOW"
                )
            ],
        )
        db_session.add(case)
    await db_session.commit()

    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    lc_tasks = [
        t
        for t in tasks
        if (t.context or {})
        .get("regression_class", "")
        .startswith("layer_concentration:")
    ]
    assert len(lc_tasks) == 0


@pytest.mark.asyncio
async def test_layer_concentration_no_escalation_when_durable(db_session):
    """3 distinct cases but all defenses still holding (no recovered_at) → no spawn."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    for i in range(3):
        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class=f"anomaly:fp_dur_{i}",
            status=RegressionCaseStatus.ACTIVE,
            signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
            last_seen_at=now - timedelta(hours=i + 1),
            hit_count=1,
            signal_first_crossed_at=now - timedelta(hours=i + 1),
            signal_first_seen_at=now - timedelta(hours=i + 1),
            fingerprint_to_fix_log=[
                # still holding → avg_hold_days=None, retrips=0 → not fragile
                _make_lc_fix_log_entry(
                    now,
                    merged_days_ago=30,
                    recovered_days_ago=None,
                    layer="BASELINE_GATE",
                )
            ],
        )
        db_session.add(case)
    await db_session.commit()

    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    lc_tasks = [
        t
        for t in tasks
        if (t.context or {})
        .get("regression_class", "")
        .startswith("layer_concentration:")
    ]
    assert len(lc_tasks) == 0


@pytest.mark.asyncio
async def test_layer_concentration_cooldown_blocks_second_tick(db_session):
    """Second run_once within cooldown window does not respawn same layer_concentration class."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    for i in range(3):
        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class=f"anomaly:fp_cool_{i}",
            status=RegressionCaseStatus.ACTIVE,
            signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
            last_seen_at=now - timedelta(hours=i + 1),
            hit_count=1,
            signal_first_crossed_at=now - timedelta(hours=i + 1),
            signal_first_seen_at=now - timedelta(hours=i + 1),
            fingerprint_to_fix_log=[
                _make_lc_fix_log_entry(
                    now, merged_days_ago=5, recovered_days_ago=4, layer="AGENT_VM"
                )
            ],
        )
        db_session.add(case)
    await db_session.commit()

    await run_once(db_session, project.id)
    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    lc_tasks = [
        t
        for t in tasks
        if (t.context or {}).get("regression_class") == "layer_concentration:AGENT_VM"
    ]
    # First tick spawned, second tick was blocked by cooldown — exactly 1 task
    assert len(lc_tasks) == 1


def test_layer_concentration_regression_class_naming():
    """regression_class format is layer_concentration:<CANONICAL_LAYER>."""
    from app.pipeline_regression_dispatcher import _CANONICAL_RECURRENCE_LAYERS

    for layer in _CANONICAL_RECURRENCE_LAYERS:
        rc = f"layer_concentration:{layer}"
        assert rc.startswith("layer_concentration:")
        assert layer in rc


# ---------------------------------------------------------------------------
# Path-classifier coverage signal — unmatched_paths persistence + alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_writes_unmatched_paths_when_classifier_returns_empty(
    db_session, monkeypatch
):
    """Empty layers + non-empty paths → unmatched_paths captured on the entry."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _backfill_layers_targeted_once

    project = make_project(github_owner="o", github_repo="r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 501,
                "merged_at": _utcnow().isoformat(),
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    async def fake_token(owner, repo):
        return "tok"

    async def fake_get_pr_files(token, owner, repo, pr_number, project_id=None):
        return [
            {"filename": "README.md"},
            {"filename": "docs/setup.md"},
        ]

    monkeypatch.setattr("app.github.get_repo_installation_token", fake_token)
    monkeypatch.setattr("app.github.get_pr_files", fake_get_pr_files)

    mutated = await _backfill_layers_targeted_once(db_session, project.id)
    assert mutated == 1

    await db_session.refresh(case)
    entry = case.fingerprint_to_fix_log[0]
    assert entry["layers_targeted"] == []
    assert entry["unmatched_paths"] == ["README.md", "docs/setup.md"]


@pytest.mark.asyncio
async def test_backfill_omits_unmatched_paths_when_classification_non_empty(
    db_session, monkeypatch
):
    """Non-empty layers result → unmatched_paths is NOT written."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _backfill_layers_targeted_once

    project = make_project(github_owner="o", github_repo="r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 502,
                "merged_at": _utcnow().isoformat(),
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    async def fake_token(owner, repo):
        return "tok"

    async def fake_get_pr_files(token, owner, repo, pr_number, project_id=None):
        return [{"filename": ".github/workflows/ci.yml"}]

    monkeypatch.setattr("app.github.get_repo_installation_token", fake_token)
    monkeypatch.setattr("app.github.get_pr_files", fake_get_pr_files)

    await _backfill_layers_targeted_once(db_session, project.id)

    await db_session.refresh(case)
    entry = case.fingerprint_to_fix_log[0]
    assert entry["layers_targeted"] == ["CI_WORKFLOW"]
    assert "unmatched_paths" not in entry


@pytest.mark.asyncio
async def test_backfill_caps_unmatched_paths_at_per_entry_bound(
    db_session, monkeypatch
):
    """unmatched_paths is capped at _UNMATCHED_PATHS_PER_ENTRY_CAP."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import (
        _UNMATCHED_PATHS_PER_ENTRY_CAP,
        _backfill_layers_targeted_once,
    )

    project = make_project(github_owner="o", github_repo="r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 503,
                "merged_at": _utcnow().isoformat(),
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    async def fake_token(owner, repo):
        return "tok"

    async def fake_get_pr_files(token, owner, repo, pr_number, project_id=None):
        return [{"filename": f"src/feature_{i}.py"} for i in range(50)]

    monkeypatch.setattr("app.github.get_repo_installation_token", fake_token)
    monkeypatch.setattr("app.github.get_pr_files", fake_get_pr_files)

    await _backfill_layers_targeted_once(db_session, project.id)

    await db_session.refresh(case)
    entry = case.fingerprint_to_fix_log[0]
    assert entry["layers_targeted"] == []
    assert len(entry["unmatched_paths"]) == _UNMATCHED_PATHS_PER_ENTRY_CAP


@pytest.mark.asyncio
async def test_backfill_empty_paths_skips_unmatched_paths_key(db_session, monkeypatch):
    """Empty PR file list → unmatched_paths key absent (no paths to surface)."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _backfill_layers_targeted_once

    project = make_project(github_owner="o", github_repo="r")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=[
            {
                "fingerprint": "sr_catch_rate",
                "fix_pr_number": 504,
                "merged_at": _utcnow().isoformat(),
            },
        ],
    )
    db_session.add(case)
    await db_session.commit()

    async def fake_token(owner, repo):
        return "tok"

    async def fake_get_pr_files(token, owner, repo, pr_number, project_id=None):
        return []

    monkeypatch.setattr("app.github.get_repo_installation_token", fake_token)
    monkeypatch.setattr("app.github.get_pr_files", fake_get_pr_files)

    await _backfill_layers_targeted_once(db_session, project.id)

    await db_session.refresh(case)
    entry = case.fingerprint_to_fix_log[0]
    assert entry["layers_targeted"] == []
    assert "unmatched_paths" not in entry


def _make_classified_case(
    project_id,
    regression_class: str,
    *,
    layers_entries: list[list[str]] | None = None,
    unmatched_entries: list[list[str]] | None = None,
    bare_entries: int = 0,
    merged_at_iso: str | None = None,
):
    """Construct a RegressionRemediationCase with mixed-classification fix-log entries."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    if merged_at_iso is None:
        merged_at_iso = _utcnow().isoformat()

    log: list[dict] = []
    pr = 1000
    for layers in layers_entries or []:
        log.append(
            {
                "fingerprint": regression_class,
                "fix_pr_number": pr,
                "merged_at": merged_at_iso,
                "layers_targeted": list(layers),
            }
        )
        pr += 1
    for paths in unmatched_entries or []:
        log.append(
            {
                "fingerprint": regression_class,
                "fix_pr_number": pr,
                "merged_at": merged_at_iso,
                "layers_targeted": [],
                "unmatched_paths": list(paths),
            }
        )
        pr += 1
    for _ in range(bare_entries):
        log.append(
            {
                "fingerprint": regression_class,
                "fix_pr_number": pr,
                "merged_at": merged_at_iso,
            }
        )
        pr += 1

    return RegressionRemediationCase(
        project_id=project_id,
        regression_class=regression_class,
        status=RegressionCaseStatus.MERGED,
        fingerprint_to_fix_log=log,
    )


@pytest.mark.asyncio
async def test_path_classifier_coverage_no_alert_when_no_qualifying_entries(
    db_session,
):
    """Project with only bare (pre-classification) entries → no alert."""
    from app.models import Alert
    from app.pipeline_regression_dispatcher import _evaluate_path_classifier_coverage

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_classified_case(project.id, "sr_catch_rate", bare_entries=10)
    db_session.add(case)
    await db_session.commit()

    await _evaluate_path_classifier_coverage(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_path_classifier_coverage_no_alert_below_min_sample(db_session):
    """Below the sample-size floor (5) — no alert even if 100% unclassified."""
    from app.models import Alert
    from app.pipeline_regression_dispatcher import _evaluate_path_classifier_coverage

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_classified_case(
        project.id,
        "sr_catch_rate",
        unmatched_entries=[["a.md"], ["b.md"], ["c.md"], ["d.md"]],
    )
    db_session.add(case)
    await db_session.commit()

    await _evaluate_path_classifier_coverage(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


@pytest.mark.asyncio
async def test_path_classifier_coverage_alert_fires_at_threshold(db_session):
    """≥ 5 qualifying entries with ≥ 20% empty → exactly one WARNING alert."""
    from app.models import Alert, AlertSeverity
    from app.pipeline_regression_dispatcher import (
        _PATH_CLASSIFIER_COVERAGE_ALERT_TITLE,
        _evaluate_path_classifier_coverage,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_classified_case(
        project.id,
        "sr_catch_rate",
        layers_entries=[["CI_WORKFLOW"], ["CI_WORKFLOW"], ["AGENT_VM"], ["AGENT_VM"]],
        unmatched_entries=[
            ["docs/a.md", "README.md"],
            ["docs/a.md"],
            ["scripts/foo.sh"],
            ["scripts/foo.sh"],
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await _evaluate_path_classifier_coverage(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.title == _PATH_CLASSIFIER_COVERAGE_ALERT_TITLE
    assert alert.severity == AlertSeverity.WARNING
    assert alert.task_id is None
    assert "docs/a.md" in alert.message
    assert "scripts/foo.sh" in alert.message


@pytest.mark.asyncio
async def test_path_classifier_coverage_alert_dedups_across_runs(db_session):
    """Running the evaluator twice produces only one open alert."""
    from app.models import Alert
    from app.pipeline_regression_dispatcher import _evaluate_path_classifier_coverage

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_classified_case(
        project.id,
        "sr_catch_rate",
        layers_entries=[["CI_WORKFLOW"]],
        unmatched_entries=[
            ["a.md"],
            ["b.md"],
            ["c.md"],
            ["d.md"],
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await _evaluate_path_classifier_coverage(db_session, project.id)
    await _evaluate_path_classifier_coverage(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_path_classifier_coverage_threshold_inclusive(db_session):
    """20% rate (1/5) is inclusive — trips the alert."""
    from app.models import Alert
    from app.pipeline_regression_dispatcher import _evaluate_path_classifier_coverage

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_classified_case(
        project.id,
        "sr_catch_rate",
        layers_entries=[
            ["CI_WORKFLOW"],
            ["CI_WORKFLOW"],
            ["CI_WORKFLOW"],
            ["AGENT_VM"],
        ],
        unmatched_entries=[["docs/foo.md"]],
    )
    db_session.add(case)
    await db_session.commit()

    await _evaluate_path_classifier_coverage(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_path_classifier_coverage_zero_empty_does_not_trip(db_session):
    """0/5 empty-layer entries does not trip — all classifications healthy."""
    from app.models import Alert
    from app.pipeline_regression_dispatcher import _evaluate_path_classifier_coverage

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = _make_classified_case(
        project.id,
        "sr_catch_rate",
        layers_entries=[
            ["CI_WORKFLOW"],
            ["CI_WORKFLOW"],
            ["CI_WORKFLOW"],
            ["AGENT_VM"],
            ["AGENT_VM"],
        ],
    )
    db_session.add(case)
    await db_session.commit()

    await _evaluate_path_classifier_coverage(db_session, project.id)

    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert alerts == []


def test_compute_unclassified_fix_coverage_excludes_pre_classification_entries():
    """Bare entries (neither layers_targeted nor unmatched_paths) are not counted."""
    from app.pipeline_regression_dispatcher import compute_unclassified_fix_coverage

    pid = uuid.uuid4()
    case = _make_classified_case(
        pid,
        "sr_catch_rate",
        layers_entries=[["CI_WORKFLOW"]],
        unmatched_entries=[["a.md"]],
        bare_entries=8,
    )

    coverage = compute_unclassified_fix_coverage([case])
    assert coverage["total_qualifying_entries"] == 2
    assert coverage["entries_with_empty_layers"] == 1


# ---------------------------------------------------------------------------
# Viewport regression evaluator tests
# ---------------------------------------------------------------------------


def _make_viewport_task(
    project_id,
    *,
    status_message: str,
    title: str = "Test Task",
    updated_at_offset_hours: int = 1,
) -> "Task":
    """Create a FAILED Task whose status_message triggers VIEWPORT_REGRESSION classification."""
    now = _utcnow()
    task = make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        title=title,
        created_at=now - timedelta(hours=updated_at_offset_hours),
        updated_at=now - timedelta(hours=updated_at_offset_hours),
    )
    task.latest_status_message = status_message
    return task


class TestViewportRegressionEvaluator:
    """Tests for _collect_viewport_failures + _evaluate_viewport_regression_signal."""

    @pytest.mark.asyncio
    async def test_viewport_failure_trips_and_spawns(self, db_session):
        """Two FAILED tasks with viewport errors on same component+width spawn a case."""
        from app.models import RegressionRemediationCase
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
            _evaluate_viewport_regression_signal,
        )

        assert _VIEWPORT_REGRESSION_MIN_FAILURES >= 2, "threshold sanity"

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): 2 element(s) overflow at width=375px:\n"
            "  <div> right=400px (limit 376px)\n"
            "Found in MessagesPanel.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["component"] == "MessagesPanel"
        assert trips[0]["width"] == "375"
        assert trips[0]["count"] >= _VIEWPORT_REGRESSION_MIN_FAILURES

        spawned = await _evaluate_viewport_regression_signal(
            db_session, project.id, spawn_budget_left=2
        )
        assert spawned == 1
        await db_session.commit()

        cases = (
            (
                await db_session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project.id,
                        RegressionRemediationCase.regression_class
                        == "viewport_regression:MessagesPanel@375",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cases) == 1

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_spawn(self, db_session):
        """A single viewport failure (below min) does not spawn."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectMinTouchTargetAt(375): 1 element(s) below 44px touch target\n"
            "Found in AlertsBanner.viewport.test.tsx"
        )
        # Add one fewer than the minimum
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES - 1):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 0

    @pytest.mark.asyncio
    async def test_unknown_component_fallback(self, db_session):
        """When the file reference is absent, component falls back to 'unknown'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = "expectNoOverflowAt(375): document.documentElement.scrollWidth=400px > 376px"
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["component"] == "unknown"
        assert trips[0]["width"] == "375"

    @pytest.mark.asyncio
    async def test_unknown_width_fallback(self, db_session):
        """When width cannot be extracted, width falls back to '0'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectnooverflowat( ): some viewport overflow detected\n"
            "Found in TaskDetailDrawerMeta.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["component"] == "TaskDetailDrawerMeta"
        assert trips[0]["width"] == "0"

    @pytest.mark.asyncio
    async def test_dedup_same_component_width_updates_not_duplicates(self, db_session):
        """A second run_once with the same viewport failure class updates the case, not duplicates."""
        from app.models import RegressionRemediationCase
        from app.pipeline_regression_dispatcher import run_once

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): 1 element(s) overflow\n"
            "Found in SearchFilterBar.viewport.test.tsx"
        )
        for _ in range(2):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        first = await run_once(db_session, project.id)
        assert first == 1
        await db_session.commit()

        # Second run: in-flight case should block re-spawn
        second = await run_once(db_session, project.id)
        assert second == 0

        cases = (
            (
                await db_session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project.id,
                        RegressionRemediationCase.regression_class.like(
                            "viewport_regression:%"
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cases) == 1

    @pytest.mark.asyncio
    async def test_overflow_only_failure_type(self, db_session):
        """Failures from expectNoOverflowAt classify as 'overflow'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectNoOverflowAt(375): 2 element(s) overflow at width=375px\n"
            "Found in MessagesPanel.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["failure_type"] == "overflow"

    @pytest.mark.asyncio
    async def test_touch_target_only_failure_type(self, db_session):
        """Failures from expectMinTouchTargetAt classify as 'touch_target'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        msg = (
            "expectMinTouchTargetAt(375): 1 element(s) below 44px touch target\n"
            "Found in AlertsBanner.viewport.test.tsx"
        )
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES):
            db_session.add(_make_viewport_task(project.id, status_message=msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["failure_type"] == "touch_target"

    @pytest.mark.asyncio
    async def test_mixed_failure_types(self, db_session):
        """When a (component, width) bucket has both overflow and touch_target failures, classify as 'mixed'."""
        from app.pipeline_regression_dispatcher import (
            _VIEWPORT_REGRESSION_MIN_FAILURES,
            _collect_viewport_failures,
        )

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        overflow_msg = (
            "expectNoOverflowAt(375): 2 element(s) overflow\n"
            "Found in TestComponent.viewport.test.tsx"
        )
        touch_target_msg = (
            "expectMinTouchTargetAt(375): 1 element(s) below 44px\n"
            "Found in TestComponent.viewport.test.tsx"
        )
        # Add at least _VIEWPORT_REGRESSION_MIN_FAILURES failures
        for _ in range(_VIEWPORT_REGRESSION_MIN_FAILURES - 1):
            db_session.add(_make_viewport_task(project.id, status_message=overflow_msg))
        db_session.add(_make_viewport_task(project.id, status_message=touch_target_msg))
        await db_session.commit()

        trips = await _collect_viewport_failures(db_session, project.id)
        assert len(trips) == 1
        assert trips[0]["failure_type"] == "mixed"


# ---------------------------------------------------------------------------
# viewport_coverage_drop signal tests
# ---------------------------------------------------------------------------


def _design_doc_with_coverage_drop(
    *,
    before: int = 10,
    after: int = 7,
    escape_hatches: int = 0,
    removed_components: list | None = None,
) -> dict:
    return {
        "viewport_coverage_snapshot": {
            "assertion_count_before": before,
            "assertion_count_after": after,
            "new_escape_hatch_count": escape_hatches,
            "removed_component_files": removed_components or [],
            "merged_pr": 1234,
        }
    }


@pytest.mark.asyncio
async def test_viewport_coverage_drop_test_deletion(db_session):
    """Signal fires when assertions are deleted (post count < pre count)."""
    project = make_project(
        design_doc=_design_doc_with_coverage_drop(before=10, after=7)
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    ctx = task.context or {}
    assert ctx["regression_class"] == "viewport_coverage_drop"
    assert ctx["signal_name"] == "viewport_coverage_drop"
    payload = task.first_payload or {}
    assert "viewport_coverage_drop" in payload.get("prompt", "")


@pytest.mark.asyncio
async def test_viewport_coverage_drop_skip_insertion(db_session):
    """Signal fires when .skip() insertion reduces effective assertion count."""
    project = make_project(design_doc=_design_doc_with_coverage_drop(before=8, after=5))
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    assert (task.context or {}).get("regression_class") == "viewport_coverage_drop"


@pytest.mark.asyncio
async def test_viewport_coverage_drop_escape_hatch_attribute(db_session):
    """Signal fires when new escape-hatch attributes are added (count stable but coverage eroded)."""
    project = make_project(
        design_doc=_design_doc_with_coverage_drop(before=10, after=10, escape_hatches=2)
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .first()
    )
    assert task is not None
    ctx = task.context or {}
    assert ctx["regression_class"] == "viewport_coverage_drop"
    payload = task.first_payload or {}
    assert "escape-hatch" in payload.get("prompt", "")


@pytest.mark.asyncio
async def test_viewport_coverage_drop_no_signal_on_component_removal(db_session):
    """Signal does NOT fire when a component file was legitimately removed."""
    project = make_project(
        design_doc=_design_doc_with_coverage_drop(
            before=10,
            after=7,
            removed_components=["admin-ui/src/components/OldWidget.tsx"],
        )
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert tasks == []


# ---------------------------------------------------------------------------
# Reviewer SLA stall signal tests
# ---------------------------------------------------------------------------


class TestReviewerSlaStallSignal:
    """Tests for _evaluate_reviewer_sla_signal."""

    @pytest.mark.asyncio
    async def test_stall_trips_and_spawns(self, db_session):
        """ACTIVE case with EVALUATING task older than threshold spawns an investigation."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        evaluating_at = now - timedelta(hours=10)
        stalled_task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=evaluating_at,
            updated_at=evaluating_at,
        )
        stalled_task.status_log = [
            {
                "t": evaluating_at.isoformat(),
                "msg": "Status changed: IN_PROGRESS → EVALUATING",
            }
        ]
        db_session.add(stalled_task)
        await db_session.commit()
        await db_session.refresh(stalled_task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=stalled_task.id,
            last_remediation_pr_number=1234,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 1
        await db_session.commit()

        tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        investigate_tasks = [
            t for t in tasks if (t.first_payload or {}).get("type") == "investigate"
        ]
        assert len(investigate_tasks) == 1
        ctx = investigate_tasks[0].context or {}
        assert ctx.get("regression_class", "").startswith("reviewer_sla_stall:")
        assert ctx.get("signal_name") == "reviewer_sla_stall"
        prompt = (investigate_tasks[0].first_payload or {}).get("prompt", "")
        assert "#1234" in prompt
        assert "EVALUATING" in prompt

    @pytest.mark.asyncio
    async def test_within_sla_does_not_spawn(self, db_session):
        """ACTIVE case with EVALUATING task younger than threshold does not spawn."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        recent_at = now - timedelta(hours=2)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=recent_at,
            updated_at=recent_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_skip_terminal_task(self, db_session):
        """Cases whose linked task is COMPLETED/FAILED/CANCELLED are skipped."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=20)
        for terminal_status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            task = make_task(
                project_id=project.id,
                status=terminal_status,
                created_at=old_at,
                updated_at=old_at,
            )
            db_session.add(task)
        await db_session.commit()

        terminal_tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        for t in terminal_tasks:
            case = RegressionRemediationCase(
                project_id=project.id,
                regression_class=f"sr_catch_rate:{t.id}",
                status=RegressionCaseStatus.ACTIVE,
                hit_count=1,
                last_remediation_task_id=t.id,
            )
            db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 5)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_skip_non_evaluating_task(self, db_session):
        """Cases whose task is IN_PROGRESS (not yet in EVALUATING) are skipped."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=20)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            created_at=old_at,
            updated_at=old_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_dedup_in_flight_investigation_blocks_second_spawn(self, db_session):
        """A second cycle with the prior SLA investigation still in-flight does not re-spawn."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        evaluating_at = now - timedelta(hours=10)
        stalled_task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=evaluating_at,
            updated_at=evaluating_at,
        )
        db_session.add(stalled_task)
        await db_session.commit()
        await db_session.refresh(stalled_task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=stalled_task.id,
        )
        db_session.add(case)
        await db_session.commit()
        await db_session.refresh(case)

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            first = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
            assert first == 1
            await db_session.commit()

            second = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
            assert second == 0
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

    @pytest.mark.asyncio
    async def test_status_log_fallback_uses_updated_at(self, db_session):
        """When status_log has no EVALUATING entry, elapsed is derived from updated_at."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=10)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=old_at,
            updated_at=old_at,
            status_log=[],
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate_fallback",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 6
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 1

    @pytest.mark.asyncio
    async def test_feature_flag_disabled_suppresses_spawns(self, db_session):
        """Setting reviewer_sla_stall_enabled=False suppresses all spawns."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=20)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=old_at,
            updated_at=old_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_enabled"] = False
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_high_threshold_suppresses_borderline_case(self, db_session):
        """Setting reviewer_sla_stall_hours=48 suppresses a case stalled for only 8h."""
        from app.models import RegressionCaseStatus, RegressionRemediationCase
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal
        import app.shared_state as _shared_state

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_at = _utcnow() - timedelta(hours=8)
        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            created_at=old_at,
            updated_at=old_at,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        case = RegressionRemediationCase(
            project_id=project.id,
            regression_class="sr_catch_rate",
            status=RegressionCaseStatus.ACTIVE,
            hit_count=1,
            last_remediation_task_id=task.id,
        )
        db_session.add(case)
        await db_session.commit()

        original_config = dict(_shared_state.runtime_config)
        _shared_state.runtime_config["reviewer_sla_stall_hours"] = 48
        try:
            spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 2)
        finally:
            _shared_state.runtime_config.clear()
            _shared_state.runtime_config.update(original_config)

        assert spawned == 0

    @pytest.mark.asyncio
    async def test_budget_zero_returns_immediately(self, db_session):
        """spawn_budget_left=0 returns 0 without touching DB."""
        from app.pipeline_regression_dispatcher import _evaluate_reviewer_sla_signal

        project = make_project()
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        spawned = await _evaluate_reviewer_sla_signal(db_session, project.id, 0)
        assert spawned == 0


# ---------------------------------------------------------------------------
# _evaluate_cross_case_fingerprint_signal integration tests
# ---------------------------------------------------------------------------


def _make_ccf_case(
    *,
    project_id,
    regression_class: str,
    symptoms_by_layer: dict,
    now,
):
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    return RegressionRemediationCase(
        project_id=project_id,
        regression_class=regression_class,
        status=RegressionCaseStatus.ACTIVE,
        signal_snapshot_at_spawn={"signal_name": "persistent_anomaly"},
        last_seen_at=now - timedelta(hours=1),
        hit_count=1,
        signal_first_crossed_at=now - timedelta(hours=1),
        signal_first_seen_at=now - timedelta(hours=1),
        symptoms_by_layer=symptoms_by_layer,
    )


def _ccf_tasks(tasks):
    return [
        t
        for t in tasks
        if (t.context or {})
        .get("regression_class", "")
        .startswith("cross_case_fingerprint:")
    ]


@pytest.mark.asyncio
async def test_cross_case_fingerprint_trips_when_distinct_classes_and_occurrences_met(
    db_session,
):
    """≥2 distinct regression_class values + ≥3 entries inside lookback → spawn."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    fp = "ImportError: cannot import name foo from bar"

    case_a = _make_ccf_case(
        project_id=project.id,
        regression_class="fix_loop_divergence:lint",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=1)).isoformat(),
                    "sample": "lint failed: " + fp,
                },
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=2)).isoformat(),
                    "sample": "lint failed again: " + fp,
                },
            ]
        },
        now=now,
    )
    case_b = _make_ccf_case(
        project_id=project.id,
        regression_class="sr_blind_spot:style",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=3)).isoformat(),
                    "sample": "style review: " + fp,
                }
            ]
        },
        now=now,
    )
    db_session.add(case_a)
    db_session.add(case_b)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned >= 1

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    ccf = _ccf_tasks(tasks)
    assert len(ccf) == 1
    task = ccf[0]
    ctx = task.context or {}
    assert ctx["signal_name"] == "cross_case_fingerprint"
    assert ctx["regression_class"].startswith("cross_case_fingerprint:")

    payload = task.first_payload or {}
    prompt = payload.get("prompt") or ""
    assert "CROSS-CASE FINGERPRINT META-INVESTIGATION" in prompt
    assert "fix_loop_divergence:lint" in prompt
    assert "sr_blind_spot:style" in prompt
    assert str(case_a.id) in prompt
    assert str(case_b.id) in prompt
    assert "is this one shared root symptom" in prompt.lower()

    # Case-level escalation_reason stamped for operator audit trail.
    from app.models import RegressionRemediationCase as _RRC

    ccf_case = (
        (
            await db_session.execute(
                select(_RRC).where(
                    _RRC.project_id == project.id,
                    _RRC.regression_class == ctx["regression_class"],
                )
            )
        )
        .scalars()
        .first()
    )
    assert ccf_case is not None
    assert ccf_case.escalation_reason == "escalated_cross_case_fingerprint"


@pytest.mark.asyncio
async def test_cross_case_fingerprint_skips_when_only_one_distinct_class(db_session):
    """3 entries of the same fingerprint, but only one regression_class → no spawn."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    fp = "TypeError: NoneType has no attr foo"

    case = _make_ccf_case(
        project_id=project.id,
        regression_class="fix_loop_divergence:lint",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=i)).isoformat(),
                    "sample": "msg",
                }
                for i in range(3)
            ]
        },
        now=now,
    )
    db_session.add(case)
    await db_session.commit()

    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(_ccf_tasks(tasks)) == 0


@pytest.mark.asyncio
async def test_cross_case_fingerprint_skips_when_below_occurrence_floor(db_session):
    """2 distinct classes but only 2 total occurrences → no spawn."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    fp = "AssertionError: expected 1 == 2"

    case_a = _make_ccf_case(
        project_id=project.id,
        regression_class="fix_loop_divergence:lint",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=1)).isoformat(),
                    "sample": "m1",
                }
            ]
        },
        now=now,
    )
    case_b = _make_ccf_case(
        project_id=project.id,
        regression_class="sr_blind_spot:style",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=2)).isoformat(),
                    "sample": "m2",
                }
            ]
        },
        now=now,
    )
    db_session.add(case_a)
    db_session.add(case_b)
    await db_session.commit()

    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(_ccf_tasks(tasks)) == 0


@pytest.mark.asyncio
async def test_cross_case_fingerprint_dedup_blocks_second_tick(db_session):
    """Cooldown / in-flight dedup blocks a second run_once from re-spawning."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    fp = "RuntimeError: import order broke under_concurrency"

    case_a = _make_ccf_case(
        project_id=project.id,
        regression_class="fix_loop_divergence:lint",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=1)).isoformat(),
                    "sample": "ms1",
                },
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=2)).isoformat(),
                    "sample": "ms2",
                },
            ]
        },
        now=now,
    )
    case_b = _make_ccf_case(
        project_id=project.id,
        regression_class="sr_blind_spot:style",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=3)).isoformat(),
                    "sample": "ms3",
                }
            ]
        },
        now=now,
    )
    db_session.add(case_a)
    db_session.add(case_b)
    await db_session.commit()

    await run_once(db_session, project.id)
    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    # First tick spawned, the second was blocked by in-flight dedup → exactly 1 task.
    assert len(_ccf_tasks(tasks)) == 1


@pytest.mark.asyncio
async def test_cross_case_fingerprint_skips_entries_outside_lookback(db_session):
    """Entries older than 14d are excluded — without that filter the signal would trip."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    fp = "ValueError: stale entry"

    case_a = _make_ccf_case(
        project_id=project.id,
        regression_class="fix_loop_divergence:lint",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=20)).isoformat(),
                    "sample": "old1",
                },
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=25)).isoformat(),
                    "sample": "old2",
                },
            ]
        },
        now=now,
    )
    case_b = _make_ccf_case(
        project_id=project.id,
        regression_class="sr_blind_spot:style",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fp,
                    "observed_at": (now - timedelta(days=30)).isoformat(),
                    "sample": "old3",
                }
            ]
        },
        now=now,
    )
    db_session.add(case_a)
    db_session.add(case_b)
    await db_session.commit()

    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(_ccf_tasks(tasks)) == 0


@pytest.mark.asyncio
async def test_cross_case_fingerprint_skips_fallback_fingerprint_equal_to_regression_class(
    db_session,
):
    """Entries whose fingerprint equals the case's regression_class are ignored as noise."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    # Both cases use a fingerprint that equals case_a's class. For case_a these
    # entries are filtered out (fingerprint == regression_class). Only case_b's
    # entry survives → one distinct contributing class, below the threshold.
    fallback_fp = "fix_loop_divergence:lint"
    case_a = _make_ccf_case(
        project_id=project.id,
        regression_class="fix_loop_divergence:lint",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fallback_fp,
                    "observed_at": (now - timedelta(days=1)).isoformat(),
                    "sample": "fallback1",
                },
                {
                    "fingerprint": fallback_fp,
                    "observed_at": (now - timedelta(days=2)).isoformat(),
                    "sample": "fallback2",
                },
            ]
        },
        now=now,
    )
    case_b = _make_ccf_case(
        project_id=project.id,
        regression_class="sr_blind_spot:style",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": fallback_fp,
                    "observed_at": (now - timedelta(days=3)).isoformat(),
                    "sample": "fallback3",
                }
            ]
        },
        now=now,
    )
    db_session.add(case_a)
    db_session.add(case_b)
    await db_session.commit()

    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(_ccf_tasks(tasks)) == 0


@pytest.mark.asyncio
async def test_cross_case_fingerprint_skips_empty_fingerprints(db_session):
    """Empty / missing fingerprint strings do not aggregate into a cluster."""
    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    case_a = _make_ccf_case(
        project_id=project.id,
        regression_class="fix_loop_divergence:lint",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "fingerprint": "",
                    "observed_at": (now - timedelta(days=1)).isoformat(),
                    "sample": "blank1",
                },
                {
                    "fingerprint": "   ",
                    "observed_at": (now - timedelta(days=2)).isoformat(),
                    "sample": "blank2",
                },
            ]
        },
        now=now,
    )
    case_b = _make_ccf_case(
        project_id=project.id,
        regression_class="sr_blind_spot:style",
        symptoms_by_layer={
            "AGENT_VM": [
                {
                    "observed_at": (now - timedelta(days=3)).isoformat(),
                    "sample": "blank3",
                }
            ]
        },
        now=now,
    )
    db_session.add(case_a)
    db_session.add(case_b)
    await db_session.commit()

    await run_once(db_session, project.id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(_ccf_tasks(tasks)) == 0


@pytest.mark.asyncio
async def test_cross_case_fingerprint_respects_spawn_budget():
    """spawn_budget_left=0 short-circuits the evaluator without DB work."""
    from app.pipeline_regression_dispatcher import (
        _evaluate_cross_case_fingerprint_signal,
    )

    spawned = await _evaluate_cross_case_fingerprint_signal(
        session=None,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        spawn_budget_left=0,
    )
    assert spawned == 0


# ---------------------------------------------------------------------------
# Agent VM resource exhaustion signal tests
# ---------------------------------------------------------------------------


def _make_vm_failed_task(
    project_id,
    *,
    failure_category: str = "AGENT_TIMEOUT",
    timeout_subtype: str = "oom",
    spot_preemption_count: int = 0,
    scope: str = "small",
    task_type: str = "create",
) -> Task:
    from app.time_utils import _utcnow as _tn

    ctx: dict = {"failure_category": failure_category, "scope": scope}
    if timeout_subtype:
        ctx["timeout_subtype"] = timeout_subtype
    if spot_preemption_count:
        ctx["spot_preemption_count"] = spot_preemption_count
    return make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        created_at=_tn() - timedelta(hours=1),
        context=ctx,
        payloads=[{"type": task_type, "prompt": "x", "base_branch": "main"}],
    )


@pytest.mark.asyncio
async def test_agent_vm_resource_zero_baseline_no_trip(db_session):
    """No FAILED tasks in window → no spawn."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.asyncio
async def test_agent_vm_resource_spike_trips(db_session):
    """Three OOM failures in 24h window trips agent_vm_resource_exhaustion."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(
            _make_vm_failed_task(
                project.id,
                failure_category="AGENT_TIMEOUT",
                timeout_subtype="oom",
            )
        )
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string()
                    == "agent_vm_resource_exhaustion",
                )
            )
        )
        .scalars()
        .first()
    )
    assert task is not None
    payload = task.first_payload or {}
    assert payload.get("type") == "investigate"
    prompt = payload.get("prompt", "")
    assert "AGENT_VM" in prompt
    assert "oom" in prompt
    ctx = task.context or {}
    assert ctx["regression_class"] == "agent_vm_resource_exhaustion"
    assert ctx["canonical_layer"] == "AGENT_VM"


@pytest.mark.asyncio
async def test_agent_vm_resource_dedup_blocks_second_spawn(db_session):
    """In-flight investigation blocks a second spawn (dedup)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(_make_vm_failed_task(project.id, timeout_subtype="oom"))
    await db_session.commit()

    first = await run_once(db_session, project.id)
    assert first == 1

    second = await run_once(db_session, project.id)
    assert second == 0

    rows = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string()
                    == "agent_vm_resource_exhaustion",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_agent_vm_resource_below_threshold_no_trip(db_session):
    """Two OOM failures (below threshold of 3) do not trip the signal."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(2):
        db_session.add(_make_vm_failed_task(project.id, timeout_subtype="oom"))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


@pytest.mark.parametrize(
    "fixture_kwargs, expected_reason",
    [
        ({"failure_category": "AGENT_TIMEOUT", "timeout_subtype": "oom"}, "oom"),
        (
            {"failure_category": "AGENT_TIMEOUT", "timeout_subtype": "heartbeat_lost"},
            "timeout",
        ),
        (
            {"failure_category": "AGENT_TIMEOUT", "timeout_subtype": "process_crash"},
            "timeout",
        ),
        (
            {
                "failure_category": "AGENT_TIMEOUT",
                "timeout_subtype": "execution_timeout",
            },
            "timeout",
        ),
        (
            {"failure_category": "AGENT_TIMEOUT", "timeout_subtype": "unknown"},
            "timeout",
        ),
        (
            {
                "failure_category": "AGENT_TIMEOUT",
                "timeout_subtype": "",
                "spot_preemption_count": 1,
            },
            "spot_preempt",
        ),
        (
            {"failure_category": "PROVISION_FAILURE", "timeout_subtype": ""},
            "vm_provisioning_failure",
        ),
    ],
)
@pytest.mark.asyncio
async def test_agent_vm_resource_exit_reason_walks_vocabulary(
    db_session, fixture_kwargs, expected_reason
):
    """Each VM-layer exit reason in VM_RESOURCE_EXIT_REASONS trips the signal
    and surfaces the correct reason in the spawned investigation prompt.

    Regression guard for the 'execution' vs 'execution_timeout' literal mismatch:
    walks every value of failure_classification.VM_RESOURCE_EXIT_REASONS so any
    future divergence between the dispatcher and the classifier helper is
    caught here, not in production.
    """
    from app.failure_classification import VM_RESOURCE_EXIT_REASONS

    assert expected_reason in VM_RESOURCE_EXIT_REASONS

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(_make_vm_failed_task(project.id, **fixture_kwargs))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.context["regression_class"].as_string()
                    == "agent_vm_resource_exhaustion",
                )
            )
        )
        .scalars()
        .first()
    )
    assert task is not None
    payload = task.first_payload or {}
    prompt = payload.get("prompt", "")
    assert expected_reason in prompt, (
        f"expected {expected_reason!r} in spawned investigation prompt; got: {prompt!r}"
    )


@pytest.mark.parametrize(
    "fixture_kwargs",
    [
        {
            "failure_category": "AGENT_TIMEOUT",
            "timeout_subtype": "cascade_cancellation",
        },
        {"failure_category": "AGENT_TIMEOUT", "timeout_subtype": "iteration_limit"},
    ],
)
@pytest.mark.asyncio
async def test_agent_vm_resource_code_level_subtypes_excluded(
    db_session, fixture_kwargs
):
    """Code-level cancellations (cascade, iteration_limit) must not be counted
    as VM-resource exhaustion — they belong to layer_concentration, not this signal."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for _ in range(3):
        db_session.add(_make_vm_failed_task(project.id, **fixture_kwargs))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0


# ---------------------------------------------------------------------------
# compute_escalation_outcome_breakdown — pure-function unit tests
# ---------------------------------------------------------------------------


def _make_escalation_case(
    escalation_reason: str | None,
    *,
    fix_log: list[dict],
    project_id: str = "00000000-0000-0000-0000-000000000001",
):
    """Build a RegressionRemediationCase for testing compute_escalation_outcome_breakdown."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    return RegressionRemediationCase(
        project_id=project_id,
        regression_class="sr_catch_rate:correctness",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        attempts=0,
        escalation_reason=escalation_reason,
        signal_snapshot_at_merge={},
        signal_snapshot_at_spawn={
            "prior_rate": 0.80,
            "current_rate": 0.50,
            "signal_name": "sr_catch_rate",
        },
        fingerprint_to_fix_log=fix_log,
    )


def test_escalation_outcome_breakdown_empty_cases():
    """Empty input returns all five buckets with merged_count=0 and durable_resolution_rate=None."""
    from app.pipeline_regression_dispatcher import (
        _ESCALATION_BUCKET_ALL_OTHER,
        _ESCALATION_BUCKET_ORDER,
        ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
        ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
        ESCALATION_REASON_ESCALATED_RECURRENCE,
        compute_escalation_outcome_breakdown,
    )

    out = compute_escalation_outcome_breakdown([])
    assert set(out["by_reason"].keys()) == set(_ESCALATION_BUCKET_ORDER)
    for bucket_name in _ESCALATION_BUCKET_ORDER:
        b = out["by_reason"][bucket_name]
        assert b["merged_count"] == 0
        assert b["retripped_within_30d_count"] == 0
        assert b["durable_resolution_rate"] is None
        assert b["median_hold_days"] is None
        assert b["in_flight_excluded_count"] == 0
    assert out["lookback_days"] == 30
    assert out["generated_at"] is not None
    # Verify expected bucket names are present
    assert ESCALATION_REASON_ESCALATED_CROSS_LAYER in out["by_reason"]
    assert ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION in out["by_reason"]
    assert ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT in out["by_reason"]
    assert ESCALATION_REASON_ESCALATED_RECURRENCE in out["by_reason"]
    assert _ESCALATION_BUCKET_ALL_OTHER in out["by_reason"]


def test_escalation_outcome_breakdown_meta_vs_other_bucketing():
    """Cases with meta escalation_reason land in the correct bucket; None → all_other."""
    from app.pipeline_regression_dispatcher import (
        _ESCALATION_BUCKET_ALL_OTHER,
        ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
        ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
        ESCALATION_REASON_ESCALATED_RECURRENCE,
        compute_escalation_outcome_breakdown,
    )

    now = _utcnow()
    merged_at = (now - timedelta(days=5)).isoformat()

    fix_log = [{"fix_pr_number": 100, "merged_at": merged_at}]
    cases = [
        _make_escalation_case(ESCALATION_REASON_ESCALATED_CROSS_LAYER, fix_log=fix_log),
        _make_escalation_case(
            ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION, fix_log=fix_log
        ),
        _make_escalation_case(
            ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT, fix_log=fix_log
        ),
        _make_escalation_case(ESCALATION_REASON_ESCALATED_RECURRENCE, fix_log=fix_log),
        _make_escalation_case(None, fix_log=fix_log),
        _make_escalation_case("attempts_exhausted", fix_log=fix_log),
    ]

    out = compute_escalation_outcome_breakdown(cases, now=now)
    by = out["by_reason"]
    assert by[ESCALATION_REASON_ESCALATED_CROSS_LAYER]["merged_count"] == 1
    assert by[ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION]["merged_count"] == 1
    assert by[ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT]["merged_count"] == 1
    assert by[ESCALATION_REASON_ESCALATED_RECURRENCE]["merged_count"] == 1
    assert by[_ESCALATION_BUCKET_ALL_OTHER]["merged_count"] == 2


def test_escalation_outcome_breakdown_lookback_window_filters():
    """Fix merged before cutoff is excluded; fix exactly at boundary is excluded by < cutoff."""
    from app.pipeline_regression_dispatcher import (
        _ESCALATION_BUCKET_ALL_OTHER,
        compute_escalation_outcome_breakdown,
    )

    now = _utcnow()
    # merged 35 days ago — outside 30d window
    old_merge = (now - timedelta(days=35)).isoformat()
    # merged 10 days ago — inside window
    recent_merge = (now - timedelta(days=10)).isoformat()

    cases = [
        _make_escalation_case(
            None, fix_log=[{"fix_pr_number": 1, "merged_at": old_merge}]
        ),
        _make_escalation_case(
            None, fix_log=[{"fix_pr_number": 2, "merged_at": recent_merge}]
        ),
    ]

    out = compute_escalation_outcome_breakdown(cases, lookback_days=30, now=now)
    b = out["by_reason"][_ESCALATION_BUCKET_ALL_OTHER]
    assert b["merged_count"] == 1  # only the recent one


def test_escalation_outcome_breakdown_retrip_detection():
    """A case with a second cycle within 30d of the first is counted as retripped."""
    from app.pipeline_regression_dispatcher import (
        ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        compute_escalation_outcome_breakdown,
    )

    now = _utcnow()
    merged1 = (now - timedelta(days=20)).isoformat()
    recovered1 = (now - timedelta(days=18)).isoformat()
    merged2 = (now - timedelta(days=10)).isoformat()  # 10d after merged1 — within 30d

    fix_log = [
        {"fix_pr_number": 1, "merged_at": merged1},
        {"recovered_at": recovered1},
        {"fix_pr_number": 2, "merged_at": merged2},
    ]
    case = _make_escalation_case(
        ESCALATION_REASON_ESCALATED_CROSS_LAYER, fix_log=fix_log
    )

    out = compute_escalation_outcome_breakdown([case], now=now)
    b = out["by_reason"][ESCALATION_REASON_ESCALATED_CROSS_LAYER]
    assert b["merged_count"] >= 1
    assert b["retripped_within_30d_count"] >= 1
    assert b["durable_resolution_rate"] == 0.0


def test_escalation_outcome_breakdown_all_durable():
    """Case with one merged+recovered cycle and no retrip → durable_resolution_rate == 1.0."""
    from app.pipeline_regression_dispatcher import (
        ESCALATION_REASON_ESCALATED_RECURRENCE,
        compute_escalation_outcome_breakdown,
    )

    now = _utcnow()
    merged_at = (now - timedelta(days=15)).isoformat()
    recovered_at = (now - timedelta(days=10)).isoformat()

    fix_log = [
        {"fix_pr_number": 1, "merged_at": merged_at},
        {"recovered_at": recovered_at},
    ]
    case = _make_escalation_case(
        ESCALATION_REASON_ESCALATED_RECURRENCE, fix_log=fix_log
    )

    out = compute_escalation_outcome_breakdown([case], now=now)
    b = out["by_reason"][ESCALATION_REASON_ESCALATED_RECURRENCE]
    assert b["merged_count"] == 1
    assert b["retripped_within_30d_count"] == 0
    assert b["durable_resolution_rate"] == 1.0
    assert b["median_hold_days"] is not None
    assert abs(b["median_hold_days"] - 5.0) < 0.1


def test_escalation_outcome_breakdown_durable_resolution_rate_mixed():
    """2 merged, 1 retripped → durable_resolution_rate == 0.5."""
    from app.pipeline_regression_dispatcher import (
        _ESCALATION_BUCKET_ALL_OTHER,
        compute_escalation_outcome_breakdown,
    )

    now = _utcnow()
    in_window = (now - timedelta(days=5)).isoformat()
    retrip = (now - timedelta(days=2)).isoformat()

    # Case 1: durable (single merged cycle, no retrip)
    case1 = _make_escalation_case(
        None, fix_log=[{"fix_pr_number": 1, "merged_at": in_window}]
    )
    # Case 2: retripped (two cycles, second within 30d)
    case2 = _make_escalation_case(
        None,
        fix_log=[
            {"fix_pr_number": 2, "merged_at": in_window},
            {"fix_pr_number": 3, "merged_at": retrip},
        ],
    )

    out = compute_escalation_outcome_breakdown([case1, case2], now=now)
    b = out["by_reason"][_ESCALATION_BUCKET_ALL_OTHER]
    assert b["merged_count"] == 2
    assert b["retripped_within_30d_count"] == 1
    assert abs(b["durable_resolution_rate"] - 0.5) < 1e-9


def test_escalation_outcome_breakdown_in_flight_excluded():
    """Cases whose only fix entry has merged_at=None are excluded from denominators."""
    from app.pipeline_regression_dispatcher import (
        _ESCALATION_BUCKET_ALL_OTHER,
        compute_escalation_outcome_breakdown,
    )

    now = _utcnow()
    in_flight = _make_escalation_case(
        None, fix_log=[{"fix_pr_number": 42, "merged_at": None}]
    )
    merged = _make_escalation_case(
        None,
        fix_log=[
            {"fix_pr_number": 1, "merged_at": (now - timedelta(days=5)).isoformat()}
        ],
    )

    out = compute_escalation_outcome_breakdown([in_flight, merged], now=now)
    b = out["by_reason"][_ESCALATION_BUCKET_ALL_OTHER]
    assert b["in_flight_excluded_count"] == 1
    assert b["merged_count"] == 1  # only the merged case counts


# ---------------------------------------------------------------------------
# Marginal-recovery follow-up investigation spawning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marginal_followup_spawns_investigate_task(db_session):
    """Marginal recovery spawns exactly one follow-up investigate task.

    The task prompt must include the pre-fix and post-fix values; the WARNING
    alert must remain the only alert (no double-fire); and the fix log must
    carry a marginal_followup_spawned entry.
    """
    from app.models import Alert, AlertSeverity, RegressionCaseStatus
    from app.pipeline_regression_dispatcher import (
        _auto_close_recovered_manual_intervention,
    )

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    case.signal_snapshot_at_merge = {"current_rate": 0.78}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    marginal_closed, confirmed_closed = await _auto_close_recovered_manual_intervention(
        db_session, project.id
    )
    assert (marginal_closed, confirmed_closed) == (1, 0)

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED
    assert case.last_remediation_result == "recovered_marginal"

    # Exactly one WARNING alert — no double-fire.
    alerts = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING

    # Exactly one follow-up investigate task spawned.
    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    followup_tasks = [
        t for t in tasks if (t.context or {}).get("signal_name") == "marginal_followup"
    ]
    assert len(followup_tasks) == 1
    ft = followup_tasks[0]
    prompt = ft.first_payload.get("prompt", "")
    assert "0.78" in prompt
    assert "0.8" in prompt

    # Fix log carries marginal_followup_spawned entry.
    log_entries = case.fingerprint_to_fix_log or []
    followup_entries = [
        e
        for e in log_entries
        if isinstance(e, dict) and e.get("type") == "marginal_followup_spawned"
    ]
    assert len(followup_entries) == 1
    assert followup_entries[0].get("spawned_at") is not None
    assert followup_entries[0].get("task_id") == str(ft.id)


@pytest.mark.asyncio
async def test_marginal_followup_cooldown_blocks_repeat_spawn(db_session):
    """A second marginal closure within the cooldown window does NOT spawn another follow-up."""
    from app.models import RegressionCaseStatus
    from app.pipeline_regression_dispatcher import (
        _auto_close_recovered_manual_intervention,
    )
    from app.shared_state import runtime_config

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    case.signal_snapshot_at_merge = {"current_rate": 0.78}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    # Pre-seed the durable cooldown anchor (1h ago → within 72h cooldown).
    case.marginal_followup_spawned_at = _utcnow() - timedelta(hours=1)
    db_session.add(case)
    await db_session.commit()

    orig = runtime_config.get("regression_marginal_followup_cooldown_hours")
    runtime_config["regression_marginal_followup_cooldown_hours"] = 72
    try:
        (
            marginal_closed,
            confirmed_closed,
        ) = await _auto_close_recovered_manual_intervention(db_session, project.id)
    finally:
        if orig is None:
            runtime_config.pop("regression_marginal_followup_cooldown_hours", None)
        else:
            runtime_config["regression_marginal_followup_cooldown_hours"] = orig

    assert (marginal_closed, confirmed_closed) == (1, 0)
    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    followup_tasks = [
        t for t in tasks if (t.context or {}).get("signal_name") == "marginal_followup"
    ]
    assert len(followup_tasks) == 0, "cooldown should have blocked the follow-up spawn"


@pytest.mark.asyncio
async def test_marginal_followup_disabled_by_config(db_session):
    """Setting regression_marginal_followup_enabled=False suppresses follow-up spawn."""
    from app.models import RegressionCaseStatus
    from app.pipeline_regression_dispatcher import (
        _auto_close_recovered_manual_intervention,
    )
    from app.shared_state import runtime_config

    project = make_project(design_doc=_design_doc_no_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = await _make_stale_case(
        db_session,
        project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=72,
        regression_class="sr_catch_rate:correctness",
    )
    case.signal_snapshot_at_merge = {"current_rate": 0.78}
    case.signal_healthy_since_at = _utcnow() - timedelta(hours=30)
    db_session.add(case)
    await db_session.commit()

    runtime_config["regression_marginal_followup_enabled"] = False
    try:
        (
            marginal_closed,
            confirmed_closed,
        ) = await _auto_close_recovered_manual_intervention(db_session, project.id)
    finally:
        runtime_config.pop("regression_marginal_followup_enabled", None)

    assert (marginal_closed, confirmed_closed) == (1, 0)
    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    followup_tasks = [
        t for t in tasks if (t.context or {}).get("signal_name") == "marginal_followup"
    ]
    assert len(followup_tasks) == 0, "disabled config should suppress follow-up spawn"
