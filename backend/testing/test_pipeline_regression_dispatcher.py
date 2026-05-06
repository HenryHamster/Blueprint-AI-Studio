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

from app.models import Project, Task, TaskStatus
from app.pipeline_regression_dispatcher import (
    _REGRESSION_COOLDOWN,
    _REGRESSION_RECOVERY_COOLDOWN,
    _WATCHDOG_ACTIVE_STALE_THRESHOLD,
    _WATCHDOG_MERGED_STALE_THRESHOLD,
    run_once,
    run_recovery_once,
    run_watchdog_once,
)
from app.time_utils import _utcnow

from testing.factories import make_project, make_task


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
            "lint": [{
                "loop": "lint",
                "iteration": 1,
                "error_tail": error_tail,
                "exit_reason": "divergence_detected",
                "started_at": (now - timedelta(minutes=2)).isoformat(),
                "ended_at": now.isoformat(),
            }],
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

    new_task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
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

    rows = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["regression_class"].as_string() == "sr_catch_rate",
        )
    )).scalars().all()
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
    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
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
            category="correctness", prior=0.80, current=0.78,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0
    rows = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
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
        "max_total_research_per_project", 5,
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

    original = _shared_state.runtime_config.get("fix_loop_divergence_fingerprint_enabled", False)
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
        db_session.add(_make_fix_loop_divergence_task(project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]))
    await db_session.commit()
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 1
        investigate = (await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.context["regression_class"].as_string().startswith("fix_loop_divergence_fingerprint:"),
            )
        )).scalars().first()
        assert investigate is not None
        prompt = (investigate.first_payload or {})["prompt"]
        assert "Normalized fingerprint:" in prompt
        assert "Sample failure tail:" in prompt
        assert "Affected scope(s): small" in prompt
        assert "backend/app/poisoned.py" in prompt
    finally:
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = original


@pytest.mark.asyncio
async def test_fix_loop_divergence_fingerprint_different_fingerprints_do_not_trip(db_session):
    import app.shared_state as _shared_state

    original = _shared_state.runtime_config.get("fix_loop_divergence_fingerprint_enabled", False)
    _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = True
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    for tail in ("AssertionError: missing foo", "AssertionError: missing bar", "RuntimeError: missing baz"):
        db_session.add(_make_fix_loop_divergence_task(project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]))
    await db_session.commit()
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 0
    finally:
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = original


@pytest.mark.asyncio
async def test_fix_loop_divergence_fingerprint_cooldown_blocks_repeat_spawn(db_session):
    import app.shared_state as _shared_state

    original = _shared_state.runtime_config.get("fix_loop_divergence_fingerprint_enabled", False)
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
        db_session.add(_make_fix_loop_divergence_task(project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]))
    await db_session.commit()
    try:
        first = await run_once(db_session, project.id)
        assert first == 1
        investigate = (await db_session.execute(
            select(Task).where(
                Task.project_id == project.id,
                Task.context["regression_class"].as_string().startswith("fix_loop_divergence_fingerprint:"),
            )
        )).scalars().first()
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
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = original


@pytest.mark.asyncio
async def test_fix_loop_divergence_fingerprint_feature_flag_gates_spawn(db_session):
    import app.shared_state as _shared_state

    original = _shared_state.runtime_config.get("fix_loop_divergence_fingerprint_enabled", False)
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
        db_session.add(_make_fix_loop_divergence_task(project.id, error_tail=tail, related_files=["backend/app/poisoned.py"]))
    await db_session.commit()
    try:
        spawned = await run_once(db_session, project.id)
        assert spawned == 0
    finally:
        _shared_state.runtime_config["fix_loop_divergence_fingerprint_enabled"] = original


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

    investigation = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["regression_class"].as_string() == "sr_catch_rate",
        )
    )).scalars().first()
    assert investigation is not None

    diagnosis_report = {
        "summary": "self-review prompt drifted; correctness category drops on regex edge cases",
        "root_cause": "self-review prompt no longer enumerates correctness sub-checks for regex edges",
        "recommended_fix": "restore the correctness checklist block in the self-review prompt template",
        "confidence": "high",
        "severity": "high",
        "affected_files": [{"path": "agent/src/prompts/self-review.ts", "issue": "missing checklist"}],
    }
    payload = SimpleNamespace(logs=json.dumps(diagnosis_report))

    await _handle_investigation_completion(investigation, payload, db_session)
    await db_session.commit()

    remediation = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.parent_task_id == investigation.id,
        )
    )).scalars().first()
    assert remediation is not None
    rpayload = remediation.first_payload or {}
    assert rpayload.get("type") == "create"
    assert "restore the correctness checklist block" in rpayload.get("prompt", "")
    assert remediation.context.get("diagnosis_task_id") == str(investigation.id)
    # Synthetic direction key carries the regression class forward.
    assert remediation.context.get("direction_id", "").startswith("pipeline_regression:")


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

    case = (await db_session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project.id,
            RegressionRemediationCase.regression_class == "sr_catch_rate",
        )
    )).scalars().first()
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
    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
    task.status = TaskStatus.COMPLETED
    task.created_at = _utcnow() - _REGRESSION_COOLDOWN - timedelta(minutes=5)
    db_session.add(task)
    await db_session.commit()

    spawned2 = await run_once(db_session, project.id)
    assert spawned2 == 1

    case = (await db_session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project.id,
            RegressionRemediationCase.regression_class == "sr_catch_rate",
        )
    )).scalars().first()
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

    db_session.add(RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hit_count=3,
    ))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
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
    sr_row = next(r for r in regressions if r["regression_class"] == "sr_catch_rate:correctness")
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
    fl_row = next(r for r in regressions if r["regression_class"] == "fix_loop_divergence:lint")
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
        "first_seen": first_seen or (now.replace(hour=0, minute=0, second=0)).isoformat(),
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

        task = (await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )).scalars().first()
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

        case = (await db_session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.project_id == project.id,
                RegressionRemediationCase.regression_class == f"anomaly:{fingerprint}",
            )
        )).scalars().first()
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

        tasks = (await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )).scalars().all()
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

        task = (await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )).scalars().first()
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
    project = make_project(design_doc={_ANOMALY_FINGERPRINTS_KEY: {fingerprint: recovered_entry}})
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
    project = make_project(design_doc={_ANOMALY_FINGERPRINTS_KEY: {fingerprint: active_entry}})
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
async def test_pipeline_regressions_endpoint_enriches_crash_investigation(client, db_session):
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
    db_session.add(Alert(
        project_id=project.id,
        severity=AlertSeverity.ERROR,
        title=f"Background service crashed: {service_name}",
        message="Service crashed with: RuntimeError",
    ))
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
        r for r in body["regressions"]
        if r["regression_class"] == f"background_service_crash:{service_name}"
    ]
    assert len(crash_rows) == 1
    crash = crash_rows[0]
    assert crash["severity"] == "critical"
    assert crash["spawned_task_id"] == str(inflight.id)
    assert crash["spawned_task_status"] == "IN_PROGRESS"
    assert crash["spawned_task_title"] == f"Investigate repeated crash: {service_name} service"


# ---------------------------------------------------------------------------
# Recovery scan — run_recovery_once tests
# ---------------------------------------------------------------------------


def _merged_case(
    project_id,
    *,
    regression_class: str = "sr_catch_rate:correctness",
    attempts: int = 0,
    minutes_since_merge: int = 20,
) -> "RegressionRemediationCase":
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
        signal_snapshot_at_spawn={"prior_rate": 0.80, "current_rate": 0.50, "signal_name": "sr_catch_rate"},
        fingerprint_to_fix_log=[],
    )


@pytest.mark.asyncio
async def test_recovery_scan_closes_recovered_signal(db_session):
    """Signal no longer tripped after merge → mark_closed(), status=CLOSED, log appended."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    # Design doc shows recovery: prior ≈ current (no regression).
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.80,
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
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    # SR still regressed: 30pp drop.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.50,
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
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    # Still tripped.
    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.50,
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
            category="correctness", prior=0.80, current=0.80,
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
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    project = make_project(
        design_doc=_design_doc_with_sr_regression(
            category="correctness", prior=0.80, current=0.80,
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
            category="correctness", prior=0.80, current=0.50,
        ),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Spawn the initial investigation (creates the RegressionRemediationCase).
    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    investigation = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["regression_class"].as_string() == "sr_catch_rate",
        )
    )).scalars().first()
    assert investigation is not None

    # Complete the investigation → spawns a remediation 'create' task.
    diagnosis = {
        "summary": "sr correctness drop",
        "root_cause": "prompt drift",
        "recommended_fix": "restore checklist",
        "confidence": "high",
        "severity": "high",
        "affected_files": [{"path": "agent/src/prompts/self-review.ts", "issue": "drift"}],
    }
    await _handle_investigation_completion(
        investigation, SimpleNamespace(logs=_json.dumps(diagnosis)), db_session,
    )
    await db_session.commit()

    remediation = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.parent_task_id == investigation.id,
        )
    )).scalars().first()
    assert remediation is not None
    # regression_class must be propagated so mark_merged can stamp the snapshot.
    assert remediation.context.get("regression_class") == "sr_catch_rate"
    assert remediation.context.get("signal_snapshot"), (
        "signal_snapshot must be propagated into remediation context"
    )

    # Simulate the PR merging: stamp MERGED with the signal snapshot.
    await _close_loop_regression_case_on_merge(remediation, 9999, db_session)
    await db_session.commit()

    case = (await db_session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project.id,
            RegressionRemediationCase.regression_class == "sr_catch_rate",
        )
    )).scalars().first()
    assert case is not None
    assert case.status == RegressionCaseStatus.MERGED
    assert case.signal_snapshot_at_merge, "mark_merged must receive a non-empty snapshot"

    # Backdate the merge so recovery cooldown has elapsed.
    snap = dict(case.signal_snapshot_at_merge)
    snap["captured_at"] = (_utcnow() - timedelta(minutes=20)).isoformat()
    case.signal_snapshot_at_merge = snap
    db_session.add(case)

    # Simulate the signal recovering: update design_doc so run_recovery_once sees no regression.
    project.design_doc = _design_doc_with_sr_regression(
        category="correctness", prior=0.80, current=0.80,
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
            category="correctness", prior=0.80, current=0.50,
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

    case = (await db_session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project.id,
            RegressionRemediationCase.regression_class == "sr_catch_rate",
        )
    )).scalars().first()
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
        category="correctness", prior=0.80, current=0.80,
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
    status: "RegressionCaseStatus",
    hours_stale: int,
    regression_class: str = "sr_catch_rate:correctness",
) -> "RegressionRemediationCase":
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
    from app.models import Alert, RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_hours = int(_WATCHDOG_ACTIVE_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session, project.id,
        status=RegressionCaseStatus.ACTIVE,
        hours_stale=stale_hours,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert any("sr_catch_rate:correctness" in (a.title or "") for a in alerts)


@pytest.mark.asyncio
async def test_watchdog_merged_stale_triggers_alert(db_session):
    """MERGED case older than 6h threshold escalates to NEEDS_MANUAL_INTERVENTION and opens Alert (case b)."""
    from app.models import Alert, RegressionCaseStatus, RegressionRemediationCase

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_hours = int(_WATCHDOG_MERGED_STALE_THRESHOLD.total_seconds() / 3600) + 2
    case = await _make_stale_case(
        db_session, project.id,
        status=RegressionCaseStatus.MERGED,
        hours_stale=stale_hours,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
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
        db_session, project.id,
        status=RegressionCaseStatus.ACTIVE,
        hours_stale=1,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.ACTIVE

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert alerts == []


@pytest.mark.asyncio
async def test_watchdog_excludes_needs_manual_intervention(db_session):
    """NEEDS_MANUAL_INTERVENTION cases are excluded from watchdog processing (case d)."""
    from app.models import Alert, RegressionCaseStatus

    project = make_project(design_doc={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Already escalated — very stale but should not be touched again.
    case = await _make_stale_case(
        db_session, project.id,
        status=RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        hours_stale=48,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
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
        db_session, project.id,
        status=RegressionCaseStatus.CLOSED,
        hours_stale=48,
    )

    escalated = await run_watchdog_once(db_session, project.id)
    assert escalated == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.CLOSED

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
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
        db_session, project.id,
        status=RegressionCaseStatus.MERGED,
        hours_stale=stale_hours,
    )

    # First watchdog run — escalate the stale MERGED case.
    escalated_1 = await run_watchdog_once(db_session, project.id)
    assert escalated_1 == 1

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.needs_manual_intervention_at is not None

    alerts_1 = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts_1) == 1

    # Second watchdog run — case is already NEEDS_MANUAL_INTERVENTION, should not re-escalate.
    escalated_2 = await run_watchdog_once(db_session, project.id)
    assert escalated_2 == 0

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION

    alerts_2 = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts_2) == 1  # No new alert created


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
    db_session.add(DeploymentState(
        id=1,
        status=DeploymentStatus.MONITORING,
        deployed_at=_utcnow(),
    ))
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    assert tasks == []


@pytest.mark.asyncio
async def test_deploy_monitoring_defer_persists_alert(db_session):
    """Deferred spawn during MONITORING writes an operator-visible Alert."""
    from app.models import Alert, DeploymentState, DeploymentStatus

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    db_session.add(DeploymentState(
        id=1,
        status=DeploymentStatus.MONITORING,
        deployed_at=_utcnow(),
    ))
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert any("monitoring" in (a.title or "").lower() for a in alerts), (
        "deferred spawn must persist an Alert so operators know why the signal was not actioned"
    )


@pytest.mark.asyncio
async def test_deploy_monitoring_window_expired_allows_spawn(db_session):
    """After the 30-min defer window the dispatcher spawns even if still MONITORING."""
    from app.models import DeploymentState, DeploymentStatus
    from app.pipeline_regression_dispatcher import _DEPLOY_MONITORING_DEFER_WINDOW

    project = make_project(
        design_doc=_design_doc_with_sr_regression(category="correctness"),
    )
    db_session.add(project)
    # Deploy was 31 minutes ago — window has expired.
    db_session.add(DeploymentState(
        id=1,
        status=DeploymentStatus.MONITORING,
        deployed_at=_utcnow() - _DEPLOY_MONITORING_DEFER_WINDOW - timedelta(minutes=1),
    ))
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
    db_session.add(DeploymentState(
        id=1,
        status=DeploymentStatus.STABLE,
        deployed_at=_utcnow() - timedelta(hours=1),
    ))
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

    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
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

    case = (await db_session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project.id,
            RegressionRemediationCase.regression_class == "sr_catch_rate",
        )
    )).scalars().first()
    assert case is not None


# ---------------------------------------------------------------------------
# Context-aware second-attempt prompts
# ---------------------------------------------------------------------------


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

    db_session.add(RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.ACTIVE,
        hit_count=1,
        attempts=1,
        last_remediation_pr_number=1234,
    ))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
    prompt = (task.first_payload or {}).get("prompt", "")
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

    db_session.add(RegressionRemediationCase(
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
    ))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "PRIOR ATTEMPT CONTEXT" in prompt
    assert "#5678" in prompt
    assert "65.0%" in prompt
    assert "different angle" in prompt.lower() or "different contributing" in prompt.lower()


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

    db_session.add(RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_catch_rate",
        status=RegressionCaseStatus.ACTIVE,
        hit_count=2,
        attempts=2,
        last_remediation_pr_number=None,
    ))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
    prompt = (task.first_payload or {}).get("prompt", "")
    assert "PRIOR ATTEMPT CONTEXT" in prompt
    assert "PR #" not in prompt


@pytest.mark.asyncio
async def test_recovery_scan_bucket_class_closes_when_all_recovered(db_session):
    """Bucket-level MERGED case closes when all constituent signals recover."""
    from app.models import RegressionCaseStatus, RegressionRemediationCase

    # Both categories initially recovered (prior ≈ current).
    project = make_project(design_doc=_design_doc_with_sr_regression(
        category="correctness", prior=0.80, current=0.80,
    ))
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
        signal_snapshot_at_spawn={"prior_rate": 0.80, "current_rate": 0.50,
                                   "signal_name": "sr_catch_rate"},
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
            {"category": category, "severity": "HIGH",
             "description": f"Reviewer caught missing {category} check in func()"}
        ]
        check_results = {category: {"passed": True}}  # SR passed — miss
    elif true_positive:
        reviewer_findings = [
            {"category": category, "severity": "HIGH",
             "description": f"Reviewer confirmed {category} issue"}
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
        db_session.add(_make_sr_blind_spot_task(
            project.id, category="correctness", miss=True,
            created_at=now - timedelta(days=3),
        ))
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(_make_sr_blind_spot_task(
            project.id, category="correctness", miss=True,
            created_at=now - timedelta(days=10),
        ))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
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
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=True, created_at=now - timedelta(days=3),
        ))
    # Prior window: no reviewer findings → p_denom = 0 < _MIN_SR_SAMPLE → suppressed
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=False, created_at=now - timedelta(days=10),
        ))
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
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=True, created_at=now - timedelta(days=3),
        ))
    # Prior window: adequate samples
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=True, created_at=now - timedelta(days=10),
        ))
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
        db_session.add(make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
            context={"_self_review_outcome": sro},
            created_at=now - timedelta(days=3),
        ))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    all_tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
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
        db_session.add(make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
            context={"_self_review_outcome": sro},
            created_at=now - timedelta(days=3),
        ))
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
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=True, created_at=now - timedelta(days=3),
        ))
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=True, created_at=now - timedelta(days=10),
        ))
    await db_session.commit()

    first = await run_once(db_session, project.id)
    assert first == 1

    second = await run_once(db_session, project.id)
    assert second == 0

    all_tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    investigate_tasks = [t for t in all_tasks if (t.first_payload or {}).get("type") == "investigate"]
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
        db_session.add(_make_sr_blind_spot_task(
            project.id, category="correctness", miss=False, true_positive=True,
            created_at=now - timedelta(days=3),
        ))
    await db_session.commit()

    merged_at = (now - timedelta(minutes=20)).isoformat()
    case = RegressionRemediationCase(
        project_id=project.id,
        regression_class="sr_blind_spot:correctness",
        status=RegressionCaseStatus.MERGED,
        hit_count=1,
        signal_snapshot_at_merge={"captured_at": merged_at},
        signal_snapshot_at_spawn={
            "prior_rate": 0.80, "current_rate": 0.80, "signal_name": "sr_blind_spot",
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
    db_session.add(DeploymentState(
        id=1,
        status=DeploymentStatus.MONITORING,
        deployed_at=_ts_now(),
    ))
    await db_session.commit()
    await db_session.refresh(project)

    now = _ts_now()
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=True, created_at=now - timedelta(days=3),
        ))
    for _ in range(_MIN_SR_SAMPLE):
        db_session.add(_make_sr_blind_spot_task(
            project.id, miss=True, created_at=now - timedelta(days=10),
        ))
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 0

    all_tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    investigate_tasks = [t for t in all_tasks if (t.first_payload or {}).get("type") == "investigate"]
    assert investigate_tasks == []

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
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

    case = (await db_session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project.id,
            RegressionRemediationCase.regression_class == "sr_catch_rate",
        )
    )).scalars().first()
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

        case = (await db_session.execute(
            select(RegressionRemediationCase).where(
                RegressionRemediationCase.project_id == project.id,
                RegressionRemediationCase.regression_class == f"anomaly:{fingerprint}",
            )
        )).scalars().first()
        assert case is not None
        assert case.signal_first_seen_at is not None
        # Should match the anomaly's first_seen, not spawn time.
        delta = abs((case.signal_first_seen_at - first_seen_dt).total_seconds())
        assert delta < 2, f"signal_first_seen_at mismatch: {case.signal_first_seen_at} vs {first_seen_dt}"
    finally:
        _ss.persistent_anomalies.pop(str(project.id), None)


# ---------------------------------------------------------------------------
# Divergence detector — investigations-diverged escalation
# ---------------------------------------------------------------------------


def _diagnosis_payload(*, root_cause: str, files: list[str], confidence: str = "high") -> dict:
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
        Alert, ESCALATION_REASON_INVESTIGATIONS_DIVERGED,
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
        db_session, project.id,
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
        current, SimpleNamespace(logs=json.dumps(diagnosis)), db_session,
    )
    await db_session.commit()

    await db_session.refresh(case)
    assert case.status != RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason != ESCALATION_REASON_INVESTIGATIONS_DIVERGED

    remediation = (await db_session.execute(
        select(Task).where(Task.parent_task_id == current.id)
    )).scalars().first()
    assert remediation is not None, "converging diagnoses must still spawn remediation"

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert not any(
        "investigations diverged" in (a.message or "").lower() for a in alerts
    )


@pytest.mark.asyncio
async def test_divergence_diverging_diagnoses_escalate(db_session, mocker):
    """Two diverging diagnoses at attempts=2 → escalate; remediation spawn skipped."""
    from app.models import (
        Alert, ESCALATION_REASON_INVESTIGATIONS_DIVERGED,
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
        db_session, project.id,
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
    pre_alert_count = len((await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all())

    await _handle_investigation_completion(
        current, SimpleNamespace(logs=json.dumps(diagnosis)), db_session,
    )
    await db_session.commit()

    await db_session.refresh(case)
    assert case.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason == ESCALATION_REASON_INVESTIGATIONS_DIVERGED
    assert case.needs_manual_intervention_at is not None

    remediation = (await db_session.execute(
        select(Task).where(Task.parent_task_id == current.id)
    )).scalars().first()
    assert remediation is None, "diverged diagnoses must skip remediation spawn"

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) > pre_alert_count
    assert any(
        "investigations diverged" in (a.message or "").lower() for a in alerts
    )


@pytest.mark.asyncio
async def test_re_arm_third_attempt_stamps_attempts_exhausted():
    """Direct unit test: re_arm at attempts==2 escalates and stamps escalation_reason."""
    from app.models import (
        ESCALATION_REASON_ATTEMPTS_EXHAUSTED,
        RegressionCaseStatus, RegressionRemediationCase,
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
        db_session, project.id,
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
        current, SimpleNamespace(logs=json.dumps(diagnosis)), db_session,
    )
    await db_session.commit()

    await db_session.refresh(case)
    assert case.status != RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
    assert case.escalation_reason != ESCALATION_REASON_INVESTIGATIONS_DIVERGED

    remediation = (await db_session.execute(
        select(Task).where(Task.parent_task_id == current.id)
    )).scalars().first()
    assert remediation is not None


# ---------------------------------------------------------------------------
# Strategic cycle stall signal tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_stall_spawns_when_stall_breached_and_project_active(db_session):
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

    prior_strategic.created_at = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS + 2)
    active_task.created_at = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS - 1)
    db_session.add(prior_strategic)
    db_session.add(active_task)
    await db_session.commit()

    spawned = await run_once(db_session, project.id)
    assert spawned == 1

    tasks = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["regression_class"].as_string() == "strategic_cycle_stall",
        )
    )).scalars().all()
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

    prior_strategic.created_at = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS + 2)
    recent_strategic.created_at = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS - 1)
    active_task.created_at = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS - 2)
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

    prior_strategic.created_at = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS + 2)
    active_task.created_at = _utcnow() - timedelta(hours=_STRATEGIC_CYCLE_STALL_HOURS - 1)
    db_session.add(prior_strategic)
    db_session.add(active_task)
    await db_session.commit()

    first = await run_once(db_session, project.id)
    assert first == 1

    # Second call within cooldown window — must not double-spawn.
    second = await run_once(db_session, project.id)
    assert second == 0

    rows = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["regression_class"].as_string() == "strategic_cycle_stall",
        )
    )).scalars().all()
    assert len(rows) == 1
