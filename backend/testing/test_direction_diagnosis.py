"""Tests for _spawn_direction_diagnosis in direction_lifecycle.py.

Auto-pause on code-failure patterns (Rule 3/4) spawns an investigate task to
analyze the root cause. Verifies the guards: pause-reason type, auto-retire
threshold, circuit breaker state, 24h per-direction dedup.
"""
import pytest
from sqlalchemy import select

import app.shared_state as _ss
from app.direction_lifecycle import _spawn_direction_diagnosis
from app.models import Project, Task, TaskStatus


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
