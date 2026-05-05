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
    run_once,
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
