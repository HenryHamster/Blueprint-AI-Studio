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
    run_once,
    run_recovery_once,
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
    assert ctx["regression_class"] == "sr_catch_rate:correctness"
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
            Task.context["regression_class"].as_string() == "sr_catch_rate:correctness",
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
            Task.context["regression_class"].as_string() == "sr_catch_rate:correctness",
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
            RegressionRemediationCase.regression_class == "sr_catch_rate:correctness",
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
            RegressionRemediationCase.regression_class == "sr_catch_rate:correctness",
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
        regression_class="sr_catch_rate:correctness",
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
