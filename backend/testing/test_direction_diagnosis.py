"""Tests for _spawn_direction_diagnosis in direction_lifecycle.py.

Auto-pause on code-failure patterns (Rule 3/4) spawns an investigate task to
analyze the root cause. Verifies the guards: pause-reason type, auto-retire
threshold, circuit breaker state, 24h per-direction dedup.

Also includes end-to-end tests that exercise the full diagnosis-remediation
feedback loop: investigation completion → remediation spawn → outcome recording
→ auto-recovery → re-pause escalation.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

import app.shared_state as _ss
from app.anomaly_detection import _error_fingerprint
from app.direction_lifecycle import (
    _compute_direction_error_fingerprint,
    _process_direction_lifecycle,
    _spawn_direction_diagnosis,
)
from app.models import ArtifactType, MessageType, Project, ProjectMessage, Task, TaskStatus
from app.task_completion import _handle_investigation_completion, _record_remediation_outcome


def _direction(did: str = "d-diag-1", pause_count: int = 1) -> dict:
    return {
        "id": did,
        "text": "Improve pipeline reliability via targeted tests",
        "auto_pause_count": pause_count,
        "research_history": [
            {"tasks_created": 3, "tasks_failed": 3, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 2, "tasks_failed": 2, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 4, "tasks_failed": 3, "tasks_failed_infra": 0, "prs_merged": 0},
        ],
    }


async def _make_project(db_session) -> Project:
    project = Project(name="Diag Test", user_id="u1", autonomous_enabled=True,
                      directions=[], vision={"purpose": "t"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)
    return project


@pytest.mark.asyncio
async def test_spawns_investigate_task_on_code_failure_pause(db_session):
    project = await _make_project(db_session)
    direction = _direction()
    reason = "High code failure rate: >70% of spawned tasks failed..."
    spawned = await _spawn_direction_diagnosis(project, direction, reason, db_session)
    assert spawned is True
    assert direction["diagnosis_in_progress"] is True
    assert "diagnosis_started_at" in direction
    await db_session.commit()
    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )
    tasks = result.scalars().all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.first_payload["type"] == "investigate"
    assert t.context["direction_id"] == direction["id"]
    assert t.context["pause_reason"] == reason
    assert t.context["diagnosis_trigger"] == "direction_auto_pause"
    assert t.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_skips_for_non_code_failure_pause_reason(db_session):
    project = await _make_project(db_session)
    direction = _direction()
    spawned = await _spawn_direction_diagnosis(
        project, direction, "Saturated: last 5 research cycles...", db_session
    )
    assert spawned is False
    assert "diagnosis_in_progress" not in direction


@pytest.mark.asyncio
async def test_skips_when_direction_is_being_auto_retired(db_session):
    project = await _make_project(db_session)
    direction = _direction(pause_count=3)  # MAX_AUTO_PAUSE_CYCLES
    spawned = await _spawn_direction_diagnosis(
        project, direction, "Recent stall: last 3 research cycles...", db_session
    )
    assert spawned is False


@pytest.mark.asyncio
async def test_skips_when_circuit_breaker_open(db_session):
    project = await _make_project(db_session)
    breaker = _ss.get_circuit_breaker(str(project.id))
    breaker["state"] = "OPEN"
    try:
        spawned = await _spawn_direction_diagnosis(
            project, _direction(), "High code failure rate: ...", db_session
        )
        assert spawned is False
    finally:
        _ss.circuit_breakers.pop(str(project.id), None)


@pytest.mark.asyncio
async def test_dedup_skips_when_recent_investigate_exists(db_session):
    project = await _make_project(db_session)
    direction = _direction(did="d-dedup-1")
    reason = "High code failure rate: ..."
    assert await _spawn_direction_diagnosis(project, direction, reason, db_session) is True
    await db_session.commit()
    # Second call within 24h must not spawn again
    direction.pop("diagnosis_in_progress", None)
    direction.pop("diagnosis_started_at", None)
    assert await _spawn_direction_diagnosis(project, direction, reason, db_session) is False


@pytest.mark.asyncio
async def test_skips_when_direction_id_missing(db_session):
    project = await _make_project(db_session)
    direction = {"text": "no id", "research_history": []}
    spawned = await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session
    )
    assert spawned is False


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end diagnosis-remediation cycle tests
#
# These cover the full feedback loop that connects diagnosis to remediation
# to effectiveness tracking — validating that individual functions compose
# correctly across module boundaries (direction_lifecycle, task_completion,
# notification_dispatch).
# ─────────────────────────────────────────────────────────────────────────────


_HIGH_CONFIDENCE_REPORT = {
    "summary": "Tasks fail due to missing lock in _record_direction_result",
    "root_cause": (
        "Concurrent writers race on the direction dict, causing one writer's "
        "update to overwrite the other. The bug is in shared_state."
    ),
    "affected_files": [
        {"path": "backend/app/shared_state.py", "issue": "missing lock"},
    ],
    "recommended_fix": (
        "Wrap record_direction_result body in the existing asyncio.Lock held "
        "for per-project state."
    ),
    "severity": "high",
    "confidence": "high",
}

_STRUCTURAL_REPORT = {
    "summary": "Direction is too broad to actionably remediate",
    "root_cause": (
        "The direction scope is too broad — it conflates two unrelated "
        "concerns (observability and resource pooling) in a single direction."
    ),
    "affected_files": [{"path": "backend/app/observability.py", "issue": "n/a"}],
    "recommended_fix": "Split the direction into two narrower directions.",
    "severity": "medium",
    "confidence": "high",
}


def _paused_direction_with_diagnosis(did: str, hours_paused: float = 2.0) -> dict:
    """Direction just auto-paused with a pending investigation in flight."""
    paused_at = (datetime.utcnow() - timedelta(hours=hours_paused)).isoformat()
    return {
        "id": did,
        "text": "Improve pipeline reliability via targeted tests",
        "status": "paused",
        "auto_paused": True,
        "auto_pause_count": 1,
        "paused_reason": (
            "High code failure rate: >70% of spawned tasks failed for code "
            "reasons in last 3 cycles"
        ),
        "paused_at": paused_at,
        "diagnosis_in_progress": True,
        "diagnosis_started_at": paused_at,
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {"tasks_created": 3, "tasks_failed": 3, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 3, "tasks_failed": 3, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 3, "tasks_failed": 3, "tasks_failed_infra": 0, "prs_merged": 0},
        ],
    }


def _investigate_payload(report: dict):
    """Build the SimpleNamespace payload _handle_investigation_completion expects."""
    return SimpleNamespace(
        logs=json.dumps(report),
        artifact_type=ArtifactType.INVESTIGATION_REPORT,
    )


async def _make_investigate_task(project: Project, direction_id: str, db_session) -> Task:
    task = Task(
        project_id=project.id,
        title=f"Investigate direction {direction_id}",
        description="Auto-pause diagnosis",
        payloads=[{"type": "investigate", "prompt": "investigate", "base_branch": "main"}],
        context={
            "direction_id": direction_id,
            "pause_reason": "High code failure rate: ...",
            "diagnosis_trigger": "direction_auto_pause",
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_full_cycle_pause_investigate_remediate_recover(db_session, mocker):
    """Investigation completes → remediation spawns → merges → direction recovers with monitor window."""
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
    direction = _paused_direction_with_diagnosis("d-e2e-full-1")
    project = Project(
        name="E2E Full Cycle",
        user_id="u-e2e",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # Stage 1: investigation completes with a high-confidence, actionable report.
    investigate_task = await _make_investigate_task(project, "d-e2e-full-1", db_session)
    await _handle_investigation_completion(
        investigate_task, _investigate_payload(_HIGH_CONFIDENCE_REPORT), db_session,
    )
    await db_session.commit()
    await db_session.refresh(project)

    remediation = (await db_session.execute(
        select(Task).where(Task.parent_task_id == investigate_task.id)
    )).scalars().first()
    assert remediation is not None, "investigation should spawn a remediation child task"
    assert remediation.first_payload["type"] == "create"
    assert remediation.first_payload["base_branch"] == "main"
    assert remediation.context["diagnosis_task_id"] == str(investigate_task.id)
    assert remediation.context["direction_id"] == "d-e2e-full-1"
    assert remediation.context["diagnosed_direction_id"] == "d-e2e-full-1"

    refreshed = project.directions[0]
    assert refreshed["remediation_task_id"] == str(remediation.id)
    assert "diagnosis_in_progress" not in refreshed

    # Stage 2: remediation merges → outcome recorded on direction.
    updated = await _record_remediation_outcome(remediation, merged=True, session=db_session)
    await db_session.commit()
    await db_session.refresh(project)
    assert updated is True
    d = project.directions[0]
    assert d["remediation_attempts"] == 1
    assert d["last_remediation_result"] == "merged"
    assert d["last_remediation_task_id"] == str(remediation.id)

    # Stage 3: age paused_at beyond the 48h cooldown, clear unhealthy history,
    # then run the lifecycle → direction should recover and start the monitor window.
    directions = list(project.directions)
    directions[0]["paused_at"] = (datetime.utcnow() - timedelta(hours=50)).isoformat()
    directions[0]["research_history"] = []  # healthy — no rules trip on empty history
    directions[0].pop("possibly_saturated", None)
    project.directions = directions
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    recovered = project.directions[0]
    assert recovered["status"] == "active", "direction should auto-recover after cooldown"
    assert "auto_paused" not in recovered
    assert "paused_at" not in recovered
    assert recovered.get("remediation_monitor_started_at"), (
        "monitor window should be started for recovered direction whose remediation merged"
    )
    # Remediation tracking fields carry forward so the escalation path can reference them.
    assert recovered["last_remediation_result"] == "merged"


@pytest.mark.asyncio
async def test_re_pause_after_merged_remediation_escalates(db_session, mocker):
    """A direction re-paused while monitoring with attempts >= 3 flags intervention + posts BLOCKER."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value="fp-abc123",
    )
    # Unhealthy history trips Rule 3 (high code failure rate) — code-failure pause.
    direction = {
        "id": "d-e2e-escal-1",
        "text": "Repeatedly failing direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
        ],
        "remediation_monitor_started_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "remediation_attempts": 3,
        "last_remediation_result": "merged",
        "error_fingerprint": "fp-abc123",
    }
    project = Project(
        name="E2E Re-pause Escalation",
        user_id="u-e2e",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused"
    assert d.get("needs_manual_intervention") is True

    # Assert the BLOCKER escalation message was posted. `from_type='system'` plus
    # context.event_type='remediation_escalated' uniquely identify this path.
    messages = (await db_session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.message_type == MessageType.BLOCKER,
        )
    )).scalars().all()
    escalations = [m for m in messages if (m.context or {}).get("event_type") == "remediation_escalated"]
    assert len(escalations) == 1, "exactly one remediation_escalated BLOCKER should be posted"
    ctx = escalations[0].context
    assert ctx["attempts"] == 3
    assert ctx["failure_pattern"] == "fp-abc123"
    assert "Remediation escalation" in escalations[0].content


async def _run_escalation_scenario(db_session, mocker, *, current_fp, last_rem_fp, monitor_age_hours):
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=current_fp,
    )
    direction = {
        "id": "d-esc",
        "text": "Re-pausing direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
        ],
        "remediation_monitor_started_at": (datetime.utcnow() - timedelta(hours=monitor_age_hours)).isoformat(),
        "remediation_attempts": 3,
        "last_remediation_result": "merged",
        "last_remediation_fingerprint": last_rem_fp,
    }
    project = Project(
        name="Escalation scenario",
        user_id="u-e2e",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    return project.directions[0]


@pytest.mark.asyncio
async def test_escalation_fires_when_fingerprints_match(db_session, mocker):
    d = await _run_escalation_scenario(
        db_session, mocker, current_fp="fp-same", last_rem_fp="fp-same", monitor_age_hours=1,
    )
    assert d.get("needs_manual_intervention") is True


@pytest.mark.asyncio
async def test_escalation_skipped_when_fingerprints_differ(db_session, mocker):
    d = await _run_escalation_scenario(
        db_session, mocker, current_fp="fp-new", last_rem_fp="fp-old", monitor_age_hours=1,
    )
    assert d.get("needs_manual_intervention") is not True


@pytest.mark.asyncio
async def test_stale_monitor_cleared_without_escalation(db_session, mocker):
    d = await _run_escalation_scenario(
        db_session, mocker, current_fp="fp-same", last_rem_fp="fp-same",
        monitor_age_hours=24 * 30,
    )
    assert d.get("needs_manual_intervention") is not True
    assert "remediation_monitor_started_at" not in d


@pytest.mark.asyncio
async def test_structural_diagnosis_appends_note_no_remediation(db_session, mocker):
    """A structural diagnosis appends a direction note but does not spawn a remediation task."""
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
    direction = _paused_direction_with_diagnosis("d-e2e-struct-1")
    project = Project(
        name="E2E Structural Diagnosis",
        user_id="u-e2e",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    investigate_task = await _make_investigate_task(project, "d-e2e-struct-1", db_session)
    await _handle_investigation_completion(
        investigate_task, _investigate_payload(_STRUCTURAL_REPORT), db_session,
    )
    await db_session.commit()
    await db_session.refresh(project)

    children = (await db_session.execute(
        select(Task).where(Task.parent_task_id == investigate_task.id)
    )).scalars().all()
    assert children == [], "structural diagnosis must not spawn a remediation task"

    d = project.directions[0]
    notes = d.get("notes") or []
    assert len(notes) == 1
    assert notes[-1]["source"] == "investigation_diagnosis"
    assert "structurally misaligned" in notes[-1]["text"]
    assert "remediation_task_id" not in d
    # The in-progress flag is cleared either way so future auto-pauses can re-diagnose.
    assert "diagnosis_in_progress" not in d
    assert "diagnosis_completed_at" in d


# ─────────────────────────────────────────────────────────────────────────────
# _compute_direction_error_fingerprint
# ─────────────────────────────────────────────────────────────────────────────


async def _add_failed_task(db_session, project_id, direction_id: str, msg: str | None) -> None:
    task = Task(
        project_id=project_id,
        title="failed",
        description="",
        payloads=[{"type": "create"}],
        context={"direction_id": direction_id},
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
        latest_status_message=msg,
    )
    db_session.add(task)
    await db_session.commit()


@pytest.mark.asyncio
async def test_fingerprint_returns_none_when_no_failed_tasks(db_session):
    project = await _make_project(db_session)
    assert await _compute_direction_error_fingerprint(project.id, "d-fp-empty", db_session) is None


@pytest.mark.asyncio
async def test_fingerprint_returns_none_below_recurrence_threshold(db_session):
    project = await _make_project(db_session)
    # Only one failure with this fingerprint — below _FINGERPRINT_MIN_RECURRENCE=2.
    await _add_failed_task(db_session, project.id, "d-fp-once", "TypeError: x is None")
    assert await _compute_direction_error_fingerprint(project.id, "d-fp-once", db_session) is None


@pytest.mark.asyncio
async def test_fingerprint_returns_dominant_when_threshold_met(db_session):
    project = await _make_project(db_session)
    for _ in range(2):
        await _add_failed_task(db_session, project.id, "d-fp-dom", "AssertionError: values differ")
    expected = _error_fingerprint("AssertionError: values differ")
    assert await _compute_direction_error_fingerprint(project.id, "d-fp-dom", db_session) == expected


@pytest.mark.asyncio
async def test_fingerprint_skips_empty_status_messages(db_session):
    project = await _make_project(db_session)
    # Two empty-message failures must NOT be coerced into a shared fingerprint.
    await _add_failed_task(db_session, project.id, "d-fp-empty-msg", "")
    await _add_failed_task(db_session, project.id, "d-fp-empty-msg", None)
    assert await _compute_direction_error_fingerprint(project.id, "d-fp-empty-msg", db_session) is None


@pytest.mark.asyncio
async def test_fingerprint_picks_best_among_multiple_distinct(db_session):
    project = await _make_project(db_session)
    # Dominant pattern: 3 matches. Other pattern: 1 match (below threshold on its own).
    for _ in range(3):
        await _add_failed_task(db_session, project.id, "d-fp-best", "KeyError: 'direction_id'")
    await _add_failed_task(db_session, project.id, "d-fp-best", "ConnectionRefusedError: db down")
    expected = _error_fingerprint("KeyError: 'direction_id'")
    assert await _compute_direction_error_fingerprint(project.id, "d-fp-best", db_session) == expected


@pytest.mark.asyncio
async def test_fingerprint_filters_by_direction_id(db_session):
    # Failures on a different direction must not contribute to the target's fingerprint.
    project = await _make_project(db_session)
    msg = "CODE_ERROR: shared error text"
    await _add_failed_task(db_session, project.id, "d-other", msg)
    await _add_failed_task(db_session, project.id, "d-other", msg)
    assert await _compute_direction_error_fingerprint(
        project.id, "d-target", db_session,
    ) is None
