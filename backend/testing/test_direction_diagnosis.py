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
from app.autonomous_research_diagnosis import _spawn_unproductive_diagnosis
from app.direction_lifecycle import (
    _compute_direction_error_fingerprint,
    _process_direction_lifecycle,
    _spawn_direction_diagnosis,
)
from app.models import Alert, AlertSeverity, ArtifactType, MessageType, Project, ProjectMessage, Task, TaskStatus
from app.autonomous_research import _spawn_task_failure_investigation
from app.failure_classification import (
    CODE_ERROR,
    TAXONOMY_CODE_DEFECT,
    should_trigger_code_defect_investigation,
)
from app.task_completion import (
    _handle_investigation_completion,
    _record_remediation_if_terminal,
    _record_remediation_outcome,
)
from app.time_utils import _utcnow


def _direction(did: str = "d-diag-1", pause_count: int = 1) -> dict:
    return {
        "id": did,
        "text": "Improve pipeline reliability via targeted tests",
        "auto_pause_count": pause_count,
        # Keep code_failure_pause_count mirrored with auto_pause_count so the
        # retirement-guard tests continue to exercise the guard. Diagnosis spawn
        # only happens on code-failure pause reasons, so this mirrors real state.
        "code_failure_pause_count": pause_count,
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
async def test_unproductive_diagnosis_spawns_investigate_for_non_code_pause(db_session):
    project = await _make_project(db_session)
    direction = _direction(did="d-unprod-1")
    reason = "Unproductive: 0 PRs merged from 5 research cycles (12 tasks created)"
    spawned = await _spawn_unproductive_diagnosis(project, direction, reason, db_session)
    assert spawned is True
    assert "unproductive_diagnosis_spawned_at" in direction
    await db_session.commit()
    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.first_payload["type"] == "investigate"
    assert t.first_payload.get("effort") == "low"
    assert t.context["diagnosis_trigger"] == "direction_unproductive_pause"
    assert t.context["unproductive_diagnosis_direction_id"] == "d-unprod-1"
    assert t.priority == 60


@pytest.mark.asyncio
async def test_unproductive_diagnosis_skips_code_failure_pause(db_session):
    project = await _make_project(db_session)
    direction = _direction(did="d-unprod-skip")
    spawned = await _spawn_unproductive_diagnosis(
        project, direction, "High code failure rate: ...", db_session
    )
    assert spawned is False
    assert "unproductive_diagnosis_spawned_at" not in direction


@pytest.mark.asyncio
async def test_unproductive_diagnosis_dedups_within_window(db_session):
    project = await _make_project(db_session)
    direction = _direction(did="d-unprod-dedup")
    reason = "Saturated: last 5 research cycles produced no actionable findings"
    assert await _spawn_unproductive_diagnosis(project, direction, reason, db_session) is True
    await db_session.commit()
    direction.pop("unproductive_diagnosis_spawned_at", None)
    assert await _spawn_unproductive_diagnosis(project, direction, reason, db_session) is False
    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    assert len(tasks) == 1


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
async def test_cross_direction_dedup_by_shared_fingerprint(db_session):
    """Two directions sharing a dominant error fingerprint should only produce one
    investigate task. The second direction gets a note referencing the first."""
    project = await _make_project(db_session)
    reason = "High code failure rate: ..."
    d1 = _direction(did="d-fp-share-1")
    d1["error_fingerprint"] = "fp-shared-root"
    assert await _spawn_direction_diagnosis(project, d1, reason, db_session) is True
    await db_session.commit()

    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    assert len(tasks) == 1
    existing_id = str(tasks[0].id)
    assert tasks[0].context.get("diagnosis_fingerprint") == "fp-shared-root"

    # Different direction, same fingerprint → skipped with note
    d2 = _direction(did="d-fp-share-2")
    d2["error_fingerprint"] = "fp-shared-root"
    assert await _spawn_direction_diagnosis(project, d2, reason, db_session) is False
    assert "diagnosis_in_progress" not in d2
    notes = d2.get("notes") or []
    assert len(notes) == 1
    assert notes[-1]["source"] == "diagnosis_dedup"
    assert notes[-1]["shared_investigation_task_id"] == existing_id
    assert "fp-shared-root" in notes[-1]["text"]

    # Still only one investigate task in the project
    tasks_after = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    assert len(tasks_after) == 1

    # Distinct fingerprint on a third direction still spawns
    d3 = _direction(did="d-fp-different")
    d3["error_fingerprint"] = "fp-other-root"
    assert await _spawn_direction_diagnosis(project, d3, reason, db_session) is True
    await db_session.commit()
    tasks_final = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    assert len(tasks_final) == 2


@pytest.mark.asyncio
async def test_dedup_across_auto_pause_and_hold_paths(db_session):
    """_spawn_direction_diagnosis (auto-pause path) and _spawn_direction_hold_investigation
    (hold path) must dedup against EACH OTHER, not just within their own path. Both
    spawn investigate-type tasks targeting the same direction_id, so a duplicate from
    one path must block a second spawn from the other.
    """
    import uuid as _uuid
    from app.autonomous_research import _spawn_direction_hold_investigation
    from app.shared_state import (
        DIRECTION_HOLD_THRESHOLD,
        direction_failure_streaks,
        record_direction_result,
    )

    direction_failure_streaks.clear()
    try:
        # ───────── direction A: hold-path task already exists, auto-pause must skip
        direction_a_id = "d-cross-dedup-a"
        project_a = Project(
            name="Cross Dedup A",
            user_id="u-cross-a",
            autonomous_enabled=True,
            directions=[{
                "id": direction_a_id,
                "text": "Cross-path direction A",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "auto_pause_count": 1,
                "research_history": [
                    {"tasks_created": 3, "tasks_failed": 3, "tasks_failed_infra": 0, "prs_merged": 0},
                ],
            }],
            vision={"purpose": "t"},
        )
        db_session.add(project_a)
        await db_session.commit()
        await db_session.refresh(project_a)
        _ss.circuit_breakers.pop(str(project_a.id), None)

        hold_task = Task(
            project_id=project_a.id,
            title="Hold-path investigation",
            payloads=[{"type": "investigate", "prompt": "hold"}],
            context={"direction_id": direction_a_id, "spawned_by": "direction_hold_investigation"},
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{_uuid.uuid4().hex[:16]}",
        )
        db_session.add(hold_task)
        await db_session.commit()

        reason = "High code failure rate: >70% of spawned tasks failed..."
        spawned = await _spawn_direction_diagnosis(
            project_a, project_a.directions[0], reason, db_session,
        )
        assert spawned is False, "auto-pause path must skip when a hold-path investigation is in-flight"
        assert "diagnosis_in_progress" not in project_a.directions[0]
        count = (await db_session.execute(
            select(Task).where(Task.project_id == project_a.id)
        )).scalars().all()
        investigates_a = [t for t in count if t.first_payload.get("type") == "investigate"]
        assert len(investigates_a) == 1

        # ───────── direction B: auto-pause task already exists, hold-path must skip
        direction_b_id = "d-cross-dedup-b"
        project_b = Project(
            name="Cross Dedup B",
            user_id="u-cross-b",
            autonomous_enabled=True,
            directions=[{
                "id": direction_b_id,
                "text": "Cross-path direction B",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
            }],
            vision={"purpose": "t"},
        )
        db_session.add(project_b)
        await db_session.commit()
        await db_session.refresh(project_b)

        diag_task = Task(
            project_id=project_b.id,
            title="Auto-pause diagnosis",
            payloads=[{"type": "investigate", "prompt": "diag"}],
            context={
                "direction_id": direction_b_id,
                "diagnosis_trigger": "direction_auto_pause",
            },
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            vm_instance_id=f"agent-vm-{_uuid.uuid4().hex[:16]}",
        )
        db_session.add(diag_task)
        await db_session.commit()

        # Place direction B on hold
        for _ in range(DIRECTION_HOLD_THRESHOLD):
            record_direction_result(
                direction_b_id, success=False, failure_class="CODE_ERROR",
                project_id=str(project_b.id),
            )

        spawned_b = await _spawn_direction_hold_investigation(project_b, db_session)
        assert spawned_b == 0, "hold path must skip when auto-pause investigation exists for same direction"
        tasks_b = (await db_session.execute(
            select(Task).where(Task.project_id == project_b.id)
        )).scalars().all()
        investigates_b = [t for t in tasks_b if t.first_payload.get("type") == "investigate"]
        assert len(investigates_b) == 1
    finally:
        direction_failure_streaks.clear()


@pytest.mark.asyncio
async def test_skips_when_direction_id_missing(db_session):
    project = await _make_project(db_session)
    direction = {"text": "no id", "research_history": []}
    spawned = await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session
    )
    assert spawned is False


@pytest.mark.asyncio
async def test_prompt_includes_last_remediation_failure_details(db_session):
    """When prior remediation failed, investigate prompt surfaces failure class + summary."""
    project = await _make_project(db_session)
    direction = _direction(did="d-with-failure-details")
    direction.update({
        "remediation_attempts": 2,
        "last_remediation_result": "failed",
        "last_remediation_at": "2026-04-17T12:00:00+00:00",
        "last_remediation_task_id": "task-prev-remediation",
        "last_remediation_fingerprint": "fp-recurring",
        "last_remediation_failure_class": "SCOPE_OVERFLOW",
        "last_remediation_error_summary": "expanded beyond 5 files during fix attempt",
    })
    reason = "High code failure rate: >70% of spawned tasks failed..."
    spawned = await _spawn_direction_diagnosis(project, direction, reason, db_session)
    assert spawned is True
    await db_session.commit()
    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    assert len(tasks) == 1
    prompt = tasks[0].first_payload["prompt"]
    assert "SCOPE_OVERFLOW" in prompt
    assert "expanded beyond 5 files during fix attempt" in prompt
    assert "Last remediation failed with" in prompt


@pytest.mark.asyncio
async def test_prompt_enriches_with_prior_remediation_task(db_session):
    """When the prior remediation task is loadable, prompt surfaces its title/description and failure guidance."""
    project = await _make_project(db_session)
    rem_task = Task(
        project_id=project.id,
        title="feat: add retry loop to flaky call",
        description="Wraps the failing call in a 3-attempt retry to paper over transient errors.",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(rem_task)
    await db_session.commit()
    await db_session.refresh(rem_task)
    direction = _direction(did="d-enrich-rem")
    direction.update({
        "remediation_attempts": 1,
        "last_remediation_result": "failed",
        "last_remediation_task_id": str(rem_task.id),
    })
    assert await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session,
    ) is True
    await db_session.commit()
    investigate = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["direction_id"].as_string() == "d-enrich-rem",
        )
    )).scalars().first()
    prompt = investigate.first_payload["prompt"]
    assert "feat: add retry loop to flaky call" in prompt
    assert "3-attempt retry to paper over transient errors" in prompt
    assert "previous remediation attempt failed" in prompt.lower()
    assert "prior diagnosis was incorrect" in prompt.lower()


@pytest.mark.asyncio
async def test_prompt_falls_back_when_remediation_task_missing(db_session):
    """Invalid/unfindable last_remediation_task_id: prompt still includes metadata + guidance, no approach block."""
    project = await _make_project(db_session)
    direction = _direction(did="d-missing-rem")
    direction.update({
        "remediation_attempts": 1,
        "last_remediation_result": "failed",
        "last_remediation_task_id": "not-a-real-uuid",
    })
    assert await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session,
    ) is True
    await db_session.commit()
    investigate = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["direction_id"].as_string() == "d-missing-rem",
        )
    )).scalars().first()
    prompt = investigate.first_payload["prompt"]
    assert "Prior remediation attempts: 1" in prompt
    assert "previous remediation attempt failed" in prompt.lower()
    assert "Prior remediation approach:" not in prompt


@pytest.mark.asyncio
async def test_prompt_omits_failure_guidance_when_last_remediation_merged(db_session):
    """A merged remediation must not trigger the 'previous remediation failed' guidance."""
    project = await _make_project(db_session)
    direction = _direction(did="d-guidance-merged")
    direction.update({
        "remediation_attempts": 1,
        "last_remediation_result": "merged",
        "last_remediation_task_id": "task-prev-merged",
    })
    assert await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session,
    ) is True
    await db_session.commit()
    investigate = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.context["direction_id"].as_string() == "d-guidance-merged",
        )
    )).scalars().first()
    prompt = investigate.first_payload["prompt"]
    assert "previous remediation attempt failed" not in prompt.lower()
    assert "Prior remediation approach:" not in prompt


@pytest.mark.asyncio
async def test_prompt_omits_failure_details_when_last_remediation_merged(db_session):
    """A merged prior remediation must not produce a failure-details line."""
    project = await _make_project(db_session)
    direction = _direction(did="d-prior-merged")
    direction.update({
        "remediation_attempts": 1,
        "last_remediation_result": "merged",
        "last_remediation_at": "2026-04-17T12:00:00+00:00",
        "last_remediation_task_id": "task-merged",
        "last_remediation_fingerprint": "fp-fixed",
    })
    reason = "High code failure rate: ..."
    spawned = await _spawn_direction_diagnosis(project, direction, reason, db_session)
    assert spawned is True
    await db_session.commit()
    tasks = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().all()
    prompt = tasks[0].first_payload["prompt"]
    assert "Last remediation failed with" not in prompt


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
async def test_auto_retire_at_threshold_creates_alert(db_session, mocker):
    """Auto-retire on reaching MAX_AUTO_PAUSE_CYCLES creates an INFO Alert and marks direction completed."""
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )
    # code_failure_pause_count=2 → increments to 3 (MAX_AUTO_PAUSE_CYCLES) on this pause.
    # Rule 3 history trips a code-failure pause reason so the counter increments.
    direction = {
        "id": "d-retire-1",
        "text": "Chronically failing direction requiring auto-retire",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_pause_count": 2,
        "code_failure_pause_count": 2,
        "research_history": [
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
        ],
    }
    project = Project(
        name="Auto-retire Alert",
        user_id="u-retire",
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
    assert d["status"] == "completed"
    assert d.get("auto_completed") is True
    assert d.get("auto_completed_reason")
    assert "auto-retired" in d["auto_completed_reason"].lower()

    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == AlertSeverity.INFO
    assert alert.title == "Direction auto-retired"
    assert "auto-retired" in alert.message.lower()


# ─────────────────────────────────────────────────────────────────────────────
# code_failure_pause_count — only code-failure auto-pauses count toward retirement.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_saturation_pauses_do_not_drive_auto_retire(db_session):
    """Three saturation auto-pauses must not retire a direction — code quality is fine."""
    # Rule 1 fires: possibly_saturated=True + 5 cycles each with tasks_created<=1.
    # "Saturated: ..." is NOT a code-failure reason, so code_failure_pause_count stays 0.
    direction = {
        "id": "d-saturated-retire",
        "text": "Saturated direction with no code issues",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_pause_count": 2,
        "code_failure_pause_count": 0,
        "possibly_saturated": True,
        "research_history": [
            {"tasks_created": 0, "tasks_failed": 0, "tasks_failed_infra": 0, "prs_merged": 0}
            for _ in range(5)
        ],
    }
    project = Project(
        name="Saturation no retire",
        user_id="u-sat",
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
    assert d["status"] == "paused", "saturation still pauses the direction"
    assert d.get("auto_completed") is not True, (
        "saturation pauses must not drive retirement even at the count threshold"
    )
    assert d.get("auto_pause_count") == 3
    assert d.get("code_failure_pause_count", 0) == 0, (
        "saturation must not increment the code-failure counter"
    )


@pytest.mark.asyncio
async def test_three_code_failure_pauses_trigger_auto_retire(db_session, mocker):
    """Three code-failure auto-pauses (code_failure_pause_count → 3) must retire the direction."""
    mocker.patch(
        "app.notification_dispatch.post_direction_milestone_message",
        new_callable=AsyncMock,
    )
    direction = {
        "id": "d-code-retire",
        "text": "Code-failure direction hitting retire threshold",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_pause_count": 2,
        "code_failure_pause_count": 2,
        "research_history": [
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0}
            for _ in range(3)
        ],
    }
    project = Project(
        name="Code retire",
        user_id="u-code",
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
    assert d["status"] == "completed"
    assert d.get("auto_completed") is True
    assert d.get("code_failure_pause_count") == 3


@pytest.mark.asyncio
async def test_mixed_pause_reasons_only_code_failures_count(db_session):
    """A direction with prior infra pauses paired with a fresh code-failure pause
    must only increment code_failure_pause_count from the code-failure event."""
    # Seed the infra-driven counter already high; the new pause is a code failure
    # so only code_failure_pause_count should advance (from 1 → 2, below threshold).
    direction = {
        "id": "d-mixed-pauses",
        "text": "Mixed pause history direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "auto_pause_count": 2,  # already seen 2 infra-driven pauses
        "code_failure_pause_count": 1,  # only one of those was code-driven
        "research_history": [
            {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0}
            for _ in range(3)
        ],
    }
    project = Project(
        name="Mixed pauses",
        user_id="u-mixed",
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
    # New code-failure pause: both counters advance by 1.
    assert d["status"] == "paused", "below retirement threshold → still paused"
    assert d.get("auto_pause_count") == 3
    assert d.get("code_failure_pause_count") == 2
    assert d.get("auto_completed") is not True


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


@pytest.mark.asyncio
async def test_stale_diagnosis_clears_all_three_fields(db_session):
    """Stale diagnosis recovery must clear diagnosis_task_id too, not just the in-progress flag + start time."""
    stale_iso = (datetime.utcnow() - timedelta(hours=5)).isoformat()
    direction = {
        "id": "d-stale-diag-1",
        "text": "Stale diagnosis direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "diagnosis_in_progress": True,
        "diagnosis_started_at": stale_iso,
        "diagnosis_task_id": "task-stale-xyz",
    }
    project = Project(
        name="Stale Diag",
        user_id="u-stale",
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
    assert "diagnosis_in_progress" not in d
    assert "diagnosis_started_at" not in d
    assert "diagnosis_task_id" not in d
    assert "diagnosis_completed_at" in d


# ─────────────────────────────────────────────────────────────────────────────
# _compute_direction_error_fingerprint
# ─────────────────────────────────────────────────────────────────────────────


async def _add_failed_task(
    db_session, project_id, direction_id: str, msg: str | None,
    created_at: datetime | None = None,
) -> None:
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
    if created_at is not None:
        task.created_at = created_at
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
    expected = _error_fingerprint("AssertionError: values differ", "test")
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


@pytest.mark.asyncio
async def test_fingerprint_uses_recent_failures_within_window(db_session):
    # Two fresh failures inside the 7-day window should dominate — stale failures
    # with a different error pattern must not drown them out.
    project = await _make_project(db_session)
    now = _utcnow()
    stale_msg = "RateLimitError: slow down"
    recent_msg = "TypeError: object has no attribute 'x'"
    for _ in range(3):
        await _add_failed_task(
            db_session, project.id, "d-fp-recency", stale_msg,
            created_at=now - timedelta(days=21),
        )
    for _ in range(2):
        await _add_failed_task(
            db_session, project.id, "d-fp-recency", recent_msg,
            created_at=now - timedelta(hours=1),
        )
    expected = _error_fingerprint(recent_msg, "integration")
    assert await _compute_direction_error_fingerprint(
        project.id, "d-fp-recency", db_session,
    ) == expected


@pytest.mark.asyncio
async def test_fingerprint_excludes_stale_failures_outside_window(db_session):
    # A single recent failure plus many old ones must yield None — the old
    # failures are filtered out and the one recent failure is below the
    # recurrence threshold, triggering the unfiltered fallback whose dominant
    # fingerprint is the recent one only if it still meets the threshold.
    project = await _make_project(db_session)
    now = _utcnow()
    stale_msg = "RateLimitError: slow down"
    recent_msg = "TypeError: only recent"
    for _ in range(4):
        await _add_failed_task(
            db_session, project.id, "d-fp-stale", stale_msg,
            created_at=now - timedelta(days=30),
        )
    await _add_failed_task(
        db_session, project.id, "d-fp-stale", recent_msg,
        created_at=now - timedelta(hours=2),
    )
    # Recent window has 1 failure (below threshold), fallback scans all 5 where
    # the stale pattern is dominant.
    assert await _compute_direction_error_fingerprint(
        project.id, "d-fp-stale", db_session,
    ) == _error_fingerprint(stale_msg)  # "RateLimitError: slow down" has no stage keyword


@pytest.mark.asyncio
async def test_fingerprint_falls_back_to_unfiltered_when_window_sparse(db_session):
    # Low-volume direction: only old failures exist. Fallback to the unfiltered
    # query must still return the dominant fingerprint so diagnosis has context.
    project = await _make_project(db_session)
    now = _utcnow()
    msg = "AssertionError: legacy regression"
    for _ in range(2):
        await _add_failed_task(
            db_session, project.id, "d-fp-sparse", msg,
            created_at=now - timedelta(days=30),
        )
    expected = _error_fingerprint(msg, "test")
    assert await _compute_direction_error_fingerprint(
        project.id, "d-fp-sparse", db_session,
    ) == expected


# ─────────────────────────────────────────────────────────────────────────────
# needs_manual_intervention clearing on recovery
# ─────────────────────────────────────────────────────────────────────────────


async def _make_intervention_project(db_session, direction: dict) -> Project:
    project = Project(
        name="Manual Intervention Clearing",
        user_id="u-intervene",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)
    return project


@pytest.mark.asyncio
async def test_auto_recovery_clears_intervention_when_fingerprint_differs(db_session):
    """Auto-recovery drops needs_manual_intervention when the last pause's fingerprint
    differs from the last remediation fingerprint — original problem is gone."""
    direction = {
        "id": "d-clear-fp-diff",
        "text": "Recovering direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate: ...",
        "paused_at": (datetime.utcnow() - timedelta(hours=50)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],  # healthy on recovery check
        "needs_manual_intervention": True,
        "error_fingerprint": "fp-new",
        "last_remediation_fingerprint": "fp-old",
        "last_remediation_result": "merged",
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active"
    assert "needs_manual_intervention" not in d


@pytest.mark.asyncio
async def test_auto_recovery_keeps_intervention_when_fingerprints_match(db_session):
    """Same fingerprint on recovery means the original problem may recur — keep the flag."""
    direction = {
        "id": "d-keep-fp-match",
        "text": "Recovering same-problem direction",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate: ...",
        "paused_at": (datetime.utcnow() - timedelta(hours=50)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "error_fingerprint": "fp-same",
        "last_remediation_fingerprint": "fp-same",
        "last_remediation_result": "merged",
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active"
    assert d.get("needs_manual_intervention") is True


@pytest.mark.asyncio
async def test_expired_monitor_on_active_direction_clears_intervention(db_session):
    """Monitor expiring on a still-active direction means the fix held — clear the flag."""
    direction = {
        "id": "d-monitor-expired",
        "text": "Direction whose fix held",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "remediation_monitor_started_at": (datetime.utcnow() - timedelta(days=8)).isoformat(),
        "last_remediation_result": "merged",
        "last_remediation_fingerprint": "fp-old",
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active"
    assert "needs_manual_intervention" not in d
    assert "remediation_monitor_started_at" not in d


@pytest.mark.asyncio
async def test_unparseable_monitor_timestamp_clears_monitor(db_session):
    """A non-ISO remediation_monitor_started_at must default to expired and clear,
    not silently keep monitoring active and risk false escalation."""
    direction = {
        "id": "d-monitor-bad-ts",
        "text": "Direction with corrupted monitor timestamp",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "remediation_monitor_started_at": "not-a-real-iso-timestamp",
        "last_remediation_result": "merged",
        "last_remediation_fingerprint": "fp-old",
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d["status"] == "active"
    assert "remediation_monitor_started_at" not in d
    assert "needs_manual_intervention" not in d


# ─────────────────────────────────────────────────────────────────────────────
# Cross-direction shared-fingerprint detection
# ─────────────────────────────────────────────────────────────────────────────


def _other_paused(did: str, fp: str | None, *, status: str = "paused", auto_paused: bool = True) -> dict:
    """Sibling direction record used to test cross-direction fingerprint annotation."""
    return {
        "id": did,
        "text": f"Sibling direction {did}",
        "status": status,
        "auto_paused": auto_paused,
        "error_fingerprint": fp,
    }


@pytest.mark.asyncio
async def test_diagnosis_annotates_shared_fingerprint_directions(db_session):
    """When other paused directions share the fingerprint, context + prompt flag the systemic pattern."""
    project = await _make_project(db_session)
    project.directions = [
        _other_paused("d-other-1", "fp-shared"),
        _other_paused("d-other-2", "fp-shared"),
        _other_paused("d-other-3", "fp-different"),
    ]
    flag_modified(project, "directions")
    await db_session.commit()
    direction = _direction(did="d-target")
    direction["error_fingerprint"] = "fp-shared"
    spawned = await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session,
    )
    assert spawned is True
    await db_session.commit()
    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
    shared = task.context.get("shared_fingerprint_directions")
    assert shared is not None
    shared_ids = {entry["id"] for entry in shared}
    assert shared_ids == {"d-other-1", "d-other-2"}
    # Each entry must carry id + text so the diagnosis agent can reference sibling directions by name.
    for entry in shared:
        assert set(entry.keys()) == {"id", "text"}
        assert entry["text"], "text field should be populated from sibling direction"
    prompt = task.first_payload["prompt"]
    assert "SYSTEMIC" in prompt
    # Prompt should surface the sibling count and explicit direction IDs so the agent
    # knows which directions co-fail on this root cause.
    assert "2 other auto-paused direction(s)" in prompt
    assert "d-other-1" in prompt and "d-other-2" in prompt
    assert "d-other-3" not in prompt
    # Sibling text (not just id) must appear so the agent can judge which
    # subsystems are affected and scope the systemic investigation accordingly.
    assert "Sibling direction d-other-1" in prompt
    assert "Sibling direction d-other-2" in prompt


@pytest.mark.asyncio
async def test_diagnosis_omits_shared_directions_when_fingerprints_unique(db_session):
    """No other directions share the fingerprint → no annotation, no SYSTEMIC hint."""
    project = await _make_project(db_session)
    project.directions = [
        _other_paused("d-other-1", "fp-different"),
        _other_paused("d-other-2", None),
    ]
    flag_modified(project, "directions")
    await db_session.commit()
    direction = _direction(did="d-target-unique")
    direction["error_fingerprint"] = "fp-unique"
    assert await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session,
    ) is True
    await db_session.commit()
    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
    assert "shared_fingerprint_directions" not in task.context
    assert "SYSTEMIC" not in task.first_payload["prompt"]


@pytest.mark.asyncio
async def test_diagnosis_ignores_active_siblings_with_matching_fingerprint(db_session):
    """Only paused, auto_paused siblings count — active directions are excluded."""
    project = await _make_project(db_session)
    project.directions = [
        # Same fingerprint but still active — must be ignored.
        _other_paused("d-active", "fp-shared", status="active", auto_paused=False),
        # Paused but not auto_paused (e.g. user-paused) — also ignored.
        _other_paused("d-manual", "fp-shared", auto_paused=False),
        # Auto-retired: status flipped to "completed" on MAX_AUTO_PAUSE_CYCLES, so the
        # direction is terminal and should not be treated as a live sibling affected
        # by the same systemic pattern.
        _other_paused("d-retired", "fp-shared", status="completed"),
    ]
    flag_modified(project, "directions")
    await db_session.commit()
    direction = _direction(did="d-target-active")
    direction["error_fingerprint"] = "fp-shared"
    assert await _spawn_direction_diagnosis(
        project, direction, "High code failure rate: ...", db_session,
    ) is True
    await db_session.commit()
    task = (await db_session.execute(
        select(Task).where(Task.project_id == project.id)
    )).scalars().first()
    assert "shared_fingerprint_directions" not in task.context
    assert "SYSTEMIC" not in task.first_payload["prompt"]


@pytest.mark.asyncio
async def test_active_monitor_not_expired_keeps_intervention(db_session):
    """Active monitor within the expiry window must not clear the flag prematurely."""
    direction = {
        "id": "d-monitor-active",
        "text": "Direction still under monitoring",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "needs_manual_intervention": True,
        "remediation_monitor_started_at": (datetime.utcnow() - timedelta(hours=6)).isoformat(),
        "last_remediation_result": "merged",
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d.get("needs_manual_intervention") is True
    assert d.get("remediation_monitor_started_at") is not None


# ─────────────────────────────────────────────────────────────────────────────
# BLOCKER escalation notification retry
# ─────────────────────────────────────────────────────────────────────────────


def _re_pause_direction(did: str = "d-retry-1") -> dict:
    """Direction configured so _process_direction_lifecycle fires an escalation.

    Unhealthy research_history trips Rule 3, remediation_monitor + 3 attempts +
    matching fingerprints satisfy the escalation precondition.
    """
    return {
        "id": did,
        "text": "Direction needing escalation",
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
        "error_fingerprint": "fp-retry",
        "last_remediation_fingerprint": "fp-retry",
    }


@pytest.mark.asyncio
async def test_escalation_failure_sets_pending_flag(db_session, mocker):
    """When post_remediation_escalation_message raises, the direction is stamped pending."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value="fp-retry",
    )
    mocker.patch(
        "app.notification_dispatch.post_remediation_escalation_message",
        new_callable=AsyncMock,
        side_effect=RuntimeError("webhook down"),
    )
    project = Project(
        name="Escalation retry — initial failure",
        user_id="u-retry",
        autonomous_enabled=True,
        directions=[_re_pause_direction("d-pending-1")],
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
    assert d.get("escalation_notification_pending") is True
    assert d.get("escalation_notification_retries") == 0


@pytest.mark.asyncio
async def test_escalation_retry_succeeds_clears_flag(db_session, mocker):
    """Retry loop clears pending flag and posts the BLOCKER message on success."""
    post_mock = mocker.patch(
        "app.notification_dispatch.post_remediation_escalation_message",
        new_callable=AsyncMock,
    )
    # Direction already stamped from a prior failed escalation; paused and healthy-history
    # so no new auto-pause fires this cycle.
    direction = {
        "id": "d-retry-ok",
        "text": "Retry target",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate: ...",
        "paused_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "escalation_notification_pending": True,
        "escalation_notification_retries": 1,
        "error_fingerprint": "fp-retry",
        "remediation_attempts": 3,
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert post_mock.await_count == 1
    kwargs = post_mock.await_args.kwargs
    assert kwargs["attempts"] == 3
    assert kwargs["failure_pattern"] == "fp-retry"
    assert "escalation_notification_pending" not in d
    assert "escalation_notification_retries" not in d


@pytest.mark.asyncio
async def test_escalation_retry_max_retries_stops(db_session, mocker, caplog):
    """After _MAX_ESCALATION_NOTIFICATION_RETRIES, the retry loop logs ERROR and stops."""
    import logging as _logging
    post_mock = mocker.patch(
        "app.notification_dispatch.post_remediation_escalation_message",
        new_callable=AsyncMock,
    )
    direction = {
        "id": "d-retry-exhausted",
        "text": "Retry exhausted",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate: ...",
        "paused_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "escalation_notification_pending": True,
        "escalation_notification_retries": 3,  # already at max
        "remediation_attempts": 3,
    }
    project = await _make_intervention_project(db_session, direction)
    with caplog.at_level(_logging.ERROR, logger="app.direction_lifecycle"):
        await _process_direction_lifecycle(
            project, db_session, cooldown_minutes=240.0,
            busy_direction_ids=set(), now=datetime.utcnow(),
        )
    await db_session.refresh(project)
    d = project.directions[0]
    assert post_mock.await_count == 0, "must not retry after max retries"
    assert "escalation_notification_pending" not in d
    err_records = [
        r for r in caplog.records
        if r.levelno == _logging.ERROR and "retries exhausted" in r.message
    ]
    assert len(err_records) == 1
    rec = err_records[0]
    assert getattr(rec, "direction_id", None) == "d-retry-exhausted"
    assert getattr(rec, "retries", None) == 3


@pytest.mark.asyncio
async def test_escalation_retry_exhaustion_creates_alert(db_session, mocker):
    """When retries are exhausted, an ERROR Alert is created so operators see it."""
    mocker.patch(
        "app.notification_dispatch.post_remediation_escalation_message",
        new_callable=AsyncMock,
    )
    direction = {
        "id": "d-alert-on-exhaust",
        "text": "Exhausted target",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate: ...",
        "paused_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "escalation_notification_pending": True,
        "escalation_notification_retries": 3,
        "remediation_attempts": 3,
        "error_fingerprint": "fp-exhaust",
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.commit()
    alerts = (await db_session.execute(
        select(Alert).where(Alert.project_id == project.id)
    )).scalars().all()
    matching = [a for a in alerts if "Escalation notification delivery failed" in a.title]
    assert len(matching) == 1
    a = matching[0]
    assert a.severity == AlertSeverity.ERROR
    assert "d-alert-on-exhaust" in a.message
    assert "fp-exhaust" in a.message


@pytest.mark.asyncio
async def test_escalation_retry_failure_increments_count(db_session, mocker):
    """When the retry itself fails, retry count increments and pending stays true."""
    mocker.patch(
        "app.notification_dispatch.post_remediation_escalation_message",
        new_callable=AsyncMock,
        side_effect=RuntimeError("still down"),
    )
    direction = {
        "id": "d-retry-increment",
        "text": "Retry increment",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate: ...",
        "paused_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "escalation_notification_pending": True,
        "escalation_notification_retries": 1,
        "remediation_attempts": 3,
    }
    project = await _make_intervention_project(db_session, direction)
    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=240.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)
    d = project.directions[0]
    assert d.get("escalation_notification_pending") is True
    assert d.get("escalation_notification_retries") == 2


# ─────────────────────────────────────────────────────────────────────────────
# Partial-success detection: a remediation that materially reduces code-failure
# rate (but leaves the same dominant fingerprint) skips manual-intervention
# escalation and is annotated last_remediation_result="partial_success".
# ─────────────────────────────────────────────────────────────────────────────


from app.direction_lifecycle import _detect_partial_remediation_success  # noqa: E402


def _partial_success_direction(
    *,
    pre_rate: float,
    post_rate: float,
    remediation_hours_ago: float = 2.0,
) -> dict:
    """Build a direction whose research_history straddles last_remediation_at
    with configurable pre/post code-failure rates."""
    last_rem = datetime.utcnow() - timedelta(hours=remediation_hours_ago)
    pre_ts = (last_rem - timedelta(hours=1)).isoformat()
    post_ts = (last_rem + timedelta(minutes=15)).isoformat()

    def _entry(ts: str, rate: float) -> dict:
        # 10 tasks per cycle; `rate` fraction are code-failures, infra=0.
        failed = round(10 * rate)
        return {
            "timestamp": ts,
            "tasks_created": 10,
            "tasks_failed": failed,
            "tasks_failed_infra": 0,
            "prs_merged": 10 - failed,
        }

    return {
        "id": "d-partial",
        "text": "Partial success direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            _entry(pre_ts, pre_rate),
            _entry(post_ts, post_rate),
        ],
        "remediation_monitor_started_at": (last_rem + timedelta(minutes=1)).isoformat(),
        "remediation_attempts": 3,
        "last_remediation_result": "merged",
        "last_remediation_at": last_rem.isoformat(),
        "error_fingerprint": "fp-partial",
        "last_remediation_fingerprint": "fp-partial",
    }


def test_detect_partial_success_true_when_rate_drops_significantly():
    d = _partial_success_direction(pre_rate=0.8, post_rate=0.2)
    assert _detect_partial_remediation_success(d) is True


def test_detect_partial_success_false_when_rate_unchanged():
    d = _partial_success_direction(pre_rate=0.8, post_rate=0.8)
    assert _detect_partial_remediation_success(d) is False


def test_detect_partial_success_none_without_post_entries():
    d = _partial_success_direction(pre_rate=0.8, post_rate=0.2)
    # Drop the post-remediation entry — only pre remains; insufficient data.
    d["research_history"] = [d["research_history"][0]]
    assert _detect_partial_remediation_success(d) is None


def test_detect_partial_success_false_without_last_remediation_at():
    d = _partial_success_direction(pre_rate=0.8, post_rate=0.2)
    d.pop("last_remediation_at", None)
    # No remediation recorded — treat as "no partial success", not insufficient data.
    assert _detect_partial_remediation_success(d) is False


def test_detect_partial_success_none_with_empty_history():
    d = _partial_success_direction(pre_rate=0.8, post_rate=0.2)
    d["research_history"] = []
    assert _detect_partial_remediation_success(d) is None


@pytest.mark.asyncio
async def test_partial_success_skips_escalation(db_session, mocker):
    """Re-pause with matching fingerprint + significant rate drop => partial_success, no escalation."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value="fp-partial",
    )
    post_mock = mocker.patch(
        "app.notification_dispatch.post_remediation_escalation_message",
        new_callable=AsyncMock,
    )
    direction = _partial_success_direction(pre_rate=0.9, post_rate=0.1)
    # Rules 3/4 run against the last 3 cycles — pack recent history with code failures
    # so _check_direction_health returns a code-failure pause reason.
    recent = [
        {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
        {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
        {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
    ]
    direction["research_history"] = direction["research_history"] + recent
    project = Project(
        name="Partial success skips escalation",
        user_id="u-partial",
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
    assert d.get("needs_manual_intervention") is not True
    assert d.get("last_remediation_result") == "partial_success"
    assert post_mock.await_count == 0, "escalation notification must not fire on partial success"


@pytest.mark.asyncio
async def test_insufficient_history_skips_escalation(db_session, mocker):
    """Merged remediation with no pre-history => insufficient data, no manual-intervention flag."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value="fp-partial",
    )
    post_mock = mocker.patch(
        "app.notification_dispatch.post_remediation_escalation_message",
        new_callable=AsyncMock,
    )
    direction = _partial_success_direction(pre_rate=0.8, post_rate=0.2)
    # Simulate newly-created direction: no pre-remediation history entries.
    recent = [
        {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
        {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
        {"tasks_created": 4, "tasks_failed": 4, "tasks_failed_infra": 0, "prs_merged": 0},
    ]
    direction["research_history"] = recent
    project = Project(
        name="Insufficient history defers escalation",
        user_id="u-insufficient",
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
    assert d.get("needs_manual_intervention") is not True
    assert post_mock.await_count == 0, "escalation must not fire when history is insufficient"


@pytest.mark.asyncio
async def test_full_task_failure_remediation_loop(db_session, mocker):
    """End-to-end: failed CODE_ERROR task → investigation → remediation → merge outcome recorded.

    Exercises the detect-diagnose-fix-merge cycle through its real seams:
    ``should_trigger_code_defect_investigation`` gates spawning,
    ``_spawn_task_failure_investigation`` creates the investigate task,
    ``_handle_investigation_completion`` spawns the remediation child, and
    ``_record_remediation_if_terminal`` stamps the direction on merge.
    """
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)

    direction_id = "d-e2e-loop-1"
    project = Project(
        name="E2E Task-Failure Loop",
        user_id="u-e2e-loop",
        autonomous_enabled=True,
        directions=[{
            "id": direction_id,
            "text": "Direction under remediation",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "research_history": [],
        }],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    # Stage 1: a directed create task fails with a CODE_ERROR / code_defect signature.
    failed_task = Task(
        project_id=project.id,
        title="Create feature X",
        description="original work",
        payloads=[{"type": "create", "prompt": "add feature X", "base_branch": "main"}],
        context={"direction_id": direction_id, "scope": "small"},
        status=TaskStatus.FAILED,
        latest_status_message="AttributeError: NoneType has no 'x'",
        created_by="ORCHESTRATOR",
    )
    db_session.add(failed_task)
    await db_session.commit()
    await db_session.refresh(failed_task)

    assert should_trigger_code_defect_investigation(failed_task) is True

    # Stage 2: spawn the investigation and verify its context links back to the source.
    spawned = await _spawn_task_failure_investigation(failed_task, db_session)
    assert spawned is True
    await db_session.commit()

    investigate_task = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != failed_task.id,
        )
    )).scalars().one()
    assert investigate_task.first_payload["type"] == "investigate"
    assert investigate_task.context["direction_id"] == direction_id
    assert investigate_task.context["source_task_id"] == str(failed_task.id)
    assert investigate_task.context["failure_taxonomy"] == TAXONOMY_CODE_DEFECT
    assert investigate_task.context["last_failure_class"] == CODE_ERROR

    # Stage 3: investigation completes with a high-confidence actionable report.
    await _handle_investigation_completion(
        investigate_task, _investigate_payload(_HIGH_CONFIDENCE_REPORT), db_session,
    )
    await db_session.commit()
    await db_session.refresh(project)

    remediation = (await db_session.execute(
        select(Task).where(Task.parent_task_id == investigate_task.id)
    )).scalars().one()
    assert remediation.first_payload["type"] == "create"
    assert remediation.context["diagnosis_task_id"] == str(investigate_task.id)
    assert remediation.context["direction_id"] == direction_id
    assert remediation.context["trigger_failure_class"] == CODE_ERROR

    # Stage 4: remediation create task COMPLETED opens a PR but the true outcome
    # is driven by the review pipeline — recording is deferred until the PR
    # actually merges. Neither the create task COMPLETED nor downstream review
    # task COMPLETED should stamp the direction.
    remediation.status = TaskStatus.COMPLETED
    db_session.add(remediation)
    await db_session.commit()

    await _record_remediation_if_terminal(remediation, TaskStatus.COMPLETED, db_session)
    await db_session.commit()
    await db_session.refresh(project)
    d = next(dd for dd in project.directions if dd["id"] == direction_id)
    assert "last_remediation_result" not in d, (
        "Create task COMPLETED must NOT stamp the direction — outcome awaits merge"
    )

    review_task = Task(
        project_id=project.id,
        title="Review PR",
        description="review",
        payloads=[{"type": "review", "pr_number": 1, "branch": "b"}],
        context={
            "direction_id": direction_id,
            "diagnosis_task_id": str(investigate_task.id),
            "trigger_failure_class": CODE_ERROR,
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    await _record_remediation_if_terminal(review_task, TaskStatus.COMPLETED, db_session)
    await db_session.commit()
    await db_session.refresh(project)
    d = next(dd for dd in project.directions if dd["id"] == direction_id)
    assert "last_remediation_result" not in d, (
        "Review task COMPLETED must NOT stamp the direction — outcome awaits actual merge"
    )

    # Stage 5: PR merges — the merge success path records the outcome against
    # the original remediation task.
    await _record_remediation_outcome(remediation, merged=True, session=db_session)
    await db_session.commit()
    await db_session.refresh(project)

    d = next(dd for dd in project.directions if dd["id"] == direction_id)
    assert d["last_remediation_result"] == "merged"
    assert d["remediation_attempts"] == 1
    assert d["last_remediation_task_id"] == str(remediation.id)
    assert d["last_remediation_diagnosis_task_id"] == str(investigate_task.id)
    history = d.get("remediation_history") or []
    assert len(history) == 1
    assert history[0]["task_id"] == str(remediation.id)
    assert history[0]["result"] == "merged"
    assert history[0]["trigger_failure_class"] == CODE_ERROR


@pytest.mark.asyncio
async def test_full_task_failure_remediation_loop_failed_outcome(db_session, mocker):
    """End-to-end companion: remediation task FAILS → direction records failure.

    Exercises the same seams as ``test_full_task_failure_remediation_loop`` but
    verifies the not-merged branch of ``_record_remediation_if_terminal``:
    ``last_remediation_result='failed'`` and a history entry with
    ``result='failed'``. Complements the merged-path e2e test to ensure both
    outcomes of the detect-diagnose-fix-merge cycle are exercised together.
    """
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)

    direction_id = "d-e2e-loop-failed"
    project = Project(
        name="E2E Task-Failure Loop (failed outcome)",
        user_id="u-e2e-loop-failed",
        autonomous_enabled=True,
        directions=[{
            "id": direction_id,
            "text": "Direction under remediation (failed path)",
            "status": "active",
            "priority": "medium",
            "paths": [],
            "added_at": "2026-01-01T00:00:00",
            "research_history": [],
        }],
        vision={"purpose": "t"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)

    failed_task = Task(
        project_id=project.id,
        title="Create feature Y",
        description="original work",
        payloads=[{"type": "create", "prompt": "add feature Y", "base_branch": "main"}],
        context={"direction_id": direction_id, "scope": "small"},
        status=TaskStatus.FAILED,
        latest_status_message="TypeError: cannot concat str and int",
        created_by="ORCHESTRATOR",
    )
    db_session.add(failed_task)
    await db_session.commit()
    await db_session.refresh(failed_task)

    assert should_trigger_code_defect_investigation(failed_task) is True
    assert await _spawn_task_failure_investigation(failed_task, db_session) is True
    await db_session.commit()

    investigate_task = (await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != failed_task.id,
        )
    )).scalars().one()

    await _handle_investigation_completion(
        investigate_task, _investigate_payload(_HIGH_CONFIDENCE_REPORT), db_session,
    )
    await db_session.commit()

    remediation = (await db_session.execute(
        select(Task).where(Task.parent_task_id == investigate_task.id)
    )).scalars().one()
    assert remediation.context["diagnosis_task_id"] == str(investigate_task.id)

    # Remediation task itself fails (fix didn't land) → direction records failure.
    remediation.status = TaskStatus.FAILED
    remediation.latest_status_message = "merge conflict: fix could not be applied"
    db_session.add(remediation)
    await db_session.commit()

    await _record_remediation_if_terminal(remediation, TaskStatus.FAILED, db_session)
    await db_session.commit()
    await db_session.refresh(project)

    d = next(dd for dd in project.directions if dd["id"] == direction_id)
    assert d["last_remediation_result"] == "failed"
    assert d["remediation_attempts"] == 1
    assert d["last_remediation_task_id"] == str(remediation.id)
    assert d["last_remediation_diagnosis_task_id"] == str(investigate_task.id)
    history = d.get("remediation_history") or []
    assert len(history) == 1
    assert history[0]["task_id"] == str(remediation.id)
    assert history[0]["result"] == "failed"
