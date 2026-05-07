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

from app.models import Task, TaskStatus
from app.pipeline_regression_dispatcher import (
    _REGRESSION_COOLDOWN,
    _WATCHDOG_ACTIVE_STALE_THRESHOLD,
    _WATCHDOG_MERGED_STALE_THRESHOLD,
    get_dispatcher_cycle_records,
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
    from app.pipeline_regression_dispatcher import _signal_healthy_since

    _signal_healthy_since.clear()

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
    from app.pipeline_regression_dispatcher import _signal_healthy_since

    _signal_healthy_since.clear()

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
    key = (str(project.id), "sr_catch_rate:correctness")
    _signal_healthy_since[key] = _utcnow() - timedelta(hours=30)

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.last_remediation_result != "recovered_unattended"
    # Marker dropped because signal would trip today.
    assert key not in _signal_healthy_since

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
    from app.pipeline_regression_dispatcher import _signal_healthy_since

    _signal_healthy_since.clear()

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

    key = (str(project.id), "sr_catch_rate:correctness")
    _signal_healthy_since[key] = _utcnow() - timedelta(hours=12)

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.last_remediation_result != "recovered_unattended"
    # Marker preserved — signal is healthy, just not long enough yet.
    assert key in _signal_healthy_since

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
    from app.pipeline_regression_dispatcher import (
        _signal_first_tripped,
        _signal_healthy_since,
    )

    _signal_healthy_since.clear()
    _signal_first_tripped.clear()

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

    key = (str(project.id), "sr_catch_rate:correctness")
    _signal_healthy_since[key] = _utcnow() - timedelta(hours=30)
    # Ensure first_tripped marker is also cleared on close.
    _signal_first_tripped[key] = _utcnow() - timedelta(hours=72)

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

    # Both process-local markers cleared on close.
    assert key not in _signal_healthy_since
    assert key not in _signal_first_tripped

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
    from app.pipeline_regression_dispatcher import _signal_healthy_since

    _signal_healthy_since.clear()

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
    key = (str(project.id), "sr_catch_rate:correctness")
    _signal_healthy_since[key] = _utcnow() - timedelta(hours=30)

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
    from app.pipeline_regression_dispatcher import _signal_healthy_since
    from app.shared_state import runtime_config

    _signal_healthy_since.clear()

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
    key = (str(project.id), "sr_catch_rate:correctness")
    # 90 minutes — under the 24h default but over the override.
    _signal_healthy_since[key] = _utcnow() - timedelta(minutes=90)

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
    from app.pipeline_regression_dispatcher import (
        _signal_first_tripped,
        _signal_healthy_since,
    )
    from app.routers.admin_digest import clear_regression_manual_intervention

    _signal_healthy_since.clear()
    _signal_first_tripped.clear()

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
    key = (str(project.id), regression_class)
    # Stale healthy_since from a prior NMI lifecycle — would cause an
    # immediate auto-close on a re-escalation if not cleared by the operator
    # clear path.
    _signal_healthy_since[key] = _utcnow() - timedelta(hours=30)
    _signal_first_tripped[key] = _utcnow() - timedelta(hours=72)

    # Operator clears via the admin endpoint.
    await clear_regression_manual_intervention(
        regression_class=regression_class,
        project_id=project.id,
        session=db_session,
        current_user={},
    )
    assert key not in _signal_healthy_since
    assert key not in _signal_first_tripped

    # Re-escalate the case back to NMI (e.g. another stale-active tick).
    await db_session.refresh(case)
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
    from app.pipeline_regression_dispatcher import _signal_healthy_since

    _signal_healthy_since.clear()

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
    key = (str(project.id), "sr_catch_rate:correctness")

    closed = await run_watchdog_once(db_session, project.id)
    assert closed == 0
    # Critically: no marker should have been set, so a subsequent tick 30h
    # later would also not auto-close.
    assert key not in _signal_healthy_since

    refreshed = (
        await db_session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.id == case.id
            )
        )
    ).scalar_one()
    assert refreshed.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION


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

    merged_at = (_utcnow() - timedelta(days=1)).isoformat()
    await _seed_dispatcher_spawn_history(
        db_session,
        project.id,
        regression_class="sr_catch_rate",
        created_at=_utcnow() - timedelta(days=1, minutes=5),
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
                "recovered_at": (_utcnow() - timedelta(hours=12)).isoformat(),
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
    from app.pipeline_regression_dispatcher import _signal_first_tripped

    project = make_project(design_doc=_design_doc_with_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await run_once(db_session, project.id)
    key = (str(project.id), "sr_catch_rate")
    assert key in _signal_first_tripped

    # Backdate tracker to simulate signal tripping 3 hours before spawn.
    _signal_first_tripped[key] = _signal_first_tripped[key] - timedelta(hours=3)
    earlier = _signal_first_tripped[key]

    # Expire cooldown and close case so run_once can re-spawn.
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
    """run_recovery_once clears _signal_first_tripped when a case is closed."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase
    from app.pipeline_regression_dispatcher import _signal_first_tripped

    project = make_project(design_doc=_design_doc_with_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await run_once(db_session, project.id)
    key = (str(project.id), "sr_catch_rate")
    assert key in _signal_first_tripped

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
    case.status = RegressionCaseStatus.MERGED
    case.signal_snapshot_at_merge = {
        "captured_at": (_utcnow() - timedelta(hours=1)).isoformat()
    }
    await db_session.commit()
    await db_session.refresh(project)

    await run_recovery_once(db_session, project.id)
    assert key not in _signal_first_tripped, (
        "tracker must be cleared when signal recovers"
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
    """run_once() emits a cycle record into the ring buffer on every call."""
    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    records = get_dispatcher_cycle_records(project_id=str(project.id))
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

    records = get_dispatcher_cycle_records(project_id=str(project.id))
    last_rec = records[-1]
    rj = last_rec["rejections_by_reason"]
    assert rj["dedup"] >= 1, "dedup rejection not recorded"
    assert rj["cooldown"] == 0, "rejection wrongly attributed to cooldown"


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
    finally:
        import app.shared_state as _ss

        _ss.persistent_anomalies.pop(str(project.id), None)


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

        cross_layer_tasks = (
            (
                await db_session.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.context["cross_layer"].as_string() == "true",
                    )
                )
            )
            .scalars()
            .all()
        )
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
    import app.pipeline_regression_dispatcher as _disp
    from app.models import Alert, AlertSeverity
    from app.pipeline_regression_dispatcher import (
        _DISPATCHER_ZERO_SPAWN_STREAK_K,
        _ZERO_SPAWN_ALERT_TITLE_PREFIX,
    )

    project = make_project(design_doc=_design_doc_with_sr_regression())
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pid_str = str(project.id)
    # Inject K cycle records: all have signals_tripped>0 and spawns_emitted==0.
    for _ in range(_DISPATCHER_ZERO_SPAWN_STREAK_K):
        _disp._dispatcher_cycle_records.append(
            {
                "project_id": pid_str,
                "cycle_started_at": _utcnow().isoformat(),
                "cycle_finished_at": _utcnow().isoformat(),
                "spawns_emitted": 0,
                "signals_tripped": 1,
                "rejections_by_reason": {"cooldown": 1},
            }
        )

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
    import app.pipeline_regression_dispatcher as _disp
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

    pid_str = str(project.id)
    # Inject K-1 suppressed + 1 successful spawn → streak broken.
    for i in range(_DISPATCHER_ZERO_SPAWN_STREAK_K):
        _disp._dispatcher_cycle_records.append(
            {
                "project_id": pid_str,
                "cycle_started_at": _utcnow().isoformat(),
                "cycle_finished_at": _utcnow().isoformat(),
                "spawns_emitted": 1 if i == _DISPATCHER_ZERO_SPAWN_STREAK_K - 1 else 0,
                "signals_tripped": 1,
                "rejections_by_reason": {
                    "cooldown": 0 if i == _DISPATCHER_ZERO_SPAWN_STREAK_K - 1 else 1
                },
            }
        )

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
