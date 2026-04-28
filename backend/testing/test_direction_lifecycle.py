import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.shared_state as _ss
from app.direction_lifecycle import _detect_partial_remediation_success, _process_direction_lifecycle
from app.models import ArtifactType, Project, Task, TaskStatus
from app.task_completion import _handle_investigation_completion


def _history_entry(ts: datetime, failure_rate: float) -> dict:
    failed = round(10 * failure_rate)
    return {
        "timestamp": ts.isoformat(),
        "tasks_created": 10,
        "tasks_failed": failed,
        "tasks_failed_infra": 0,
        "prs_merged": 10 - failed,
    }


@pytest.mark.asyncio
async def test_remediation_spawn_sets_last_remediation_at_for_partial_detector(db_session, mocker):
    """D13: in-flight remediation has a boundary timestamp before merge outcome."""
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)
    direction_id = "d13-direction"
    direction = {
        "id": direction_id,
        "text": "Reduce recurring code failures",
        "status": "paused",
        "auto_paused": True,
        "paused_reason": "High code failure rate",
        "diagnosis_in_progress": True,
        "diagnosis_started_at": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [
            _history_entry(datetime.utcnow() - timedelta(days=2), 0.9),
        ],
    }
    project = Project(
        name="D13 remediation spawn",
        user_id="u-d13",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    investigation = Task(
        project_id=project.id,
        title="Investigate direction",
        payloads=[{"type": "investigate", "prompt": "investigate", "base_branch": "main"}],
        context={
            "direction_id": direction_id,
            "diagnosis_fingerprint": "fp-d13",
            "last_failure_class": "CODE_ERROR",
        },
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(investigation)
    await db_session.commit()
    await db_session.refresh(investigation)

    report = {
        "summary": "Missing lock causes recurring failed tasks",
        "root_cause": "A shared direction counter is updated without synchronization.",
        "affected_files": [{"path": "backend/app/shared_state.py", "issue": "missing lock"}],
        "recommended_fix": "Guard the shared counter update with the existing lock.",
        "severity": "high",
        "confidence": "high",
    }
    await _handle_investigation_completion(
        investigation,
        SimpleNamespace(logs=json.dumps(report), artifact_type=ArtifactType.INVESTIGATION_REPORT),
        db_session,
    )
    await db_session.commit()
    await db_session.refresh(project)

    remediation = (await db_session.execute(
        select(Task).where(Task.parent_task_id == investigation.id)
    )).scalars().one()
    refreshed = next(d for d in project.directions if d["id"] == direction_id)
    assert refreshed["last_remediation_task_id"] == str(remediation.id)
    assert refreshed["last_remediation_at"]
    assert "last_remediation_result" not in refreshed

    spawned_at = datetime.fromisoformat(refreshed["last_remediation_at"])
    refreshed["research_history"].append(
        _history_entry(spawned_at + timedelta(minutes=1), 0.1)
    )
    assert _detect_partial_remediation_success(refreshed) is True


def _cost_history(*costs: float) -> list[dict]:
    """Build a minimal research_history with estimated_cost_usd entries and healthy metrics."""
    entries = []
    for i, cost in enumerate(costs):
        entries.append({
            "timestamp": (datetime.utcnow() - timedelta(days=len(costs) - i)).isoformat(),
            "tasks_created": 2,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 2,
            "estimated_cost_usd": cost,
        })
    return entries


async def _make_cost_project(db_session, direction: dict) -> Project:
    project = Project(
        name="Cost Budget Test",
        user_id="u-cost",
        autonomous_enabled=True,
        directions=[direction],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _ss.circuit_breakers.pop(str(project.id), None)
    return project


@pytest.mark.asyncio
async def test_process_lifecycle_pauses_on_cost_budget_exceeded(db_session, mocker):
    """Direction auto-pauses with cost evidence when cumulative spend exceeds cost_budget_usd."""
    mocker.patch(
        "app.direction_lifecycle._compute_direction_error_fingerprint",
        new_callable=AsyncMock,
        return_value=None,
    )
    unproductive_diag = mocker.patch(
        "app.autonomous_research_diagnosis._spawn_unproductive_diagnosis",
        new_callable=AsyncMock,
    )
    direction = {
        "id": "d-budget-over",
        "text": "Over-budget direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "cost_budget_usd": 5.0,
        "research_history": _cost_history(3.0, 3.0),  # cumulative 6.0 > budget 5.0
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=0.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "paused", "direction should be auto-paused when cost budget exceeded"
    assert d.get("auto_paused") is True
    reason = d.get("paused_reason", "")
    assert "budget" in reason.lower() or "cost" in reason.lower(), (
        f"paused_reason should mention budget/cost, got: {reason!r}"
    )
    assert d.get("cost_pause_evidence", {}).get("cumulative_estimated_cost_usd") == 6.0
    assert d.get("cost_pause_evidence", {}).get("cost_budget_usd") == 5.0
    unproductive_diag.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_lifecycle_no_pause_when_cost_within_budget(db_session, mocker):
    """Direction stays active when cumulative spend is below cost_budget_usd."""
    direction = {
        "id": "d-budget-ok",
        "text": "Within-budget direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "cost_budget_usd": 100.0,
        "research_history": _cost_history(3.0, 3.0),  # cumulative 6.0 < budget 100.0
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=0.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", "direction should stay active when spend is within budget"


@pytest.mark.asyncio
async def test_process_lifecycle_no_cost_pause_without_budget_configured(db_session, mocker):
    """Direction without cost_budget_usd is not paused regardless of estimated spend."""
    direction = {
        "id": "d-no-budget",
        "text": "Unbounded direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        # no cost_budget_usd set
        "research_history": _cost_history(500.0, 500.0),  # high spend, no budget ceiling
    }
    project = await _make_cost_project(db_session, direction)

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=0.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    d = project.directions[0]
    assert d["status"] == "active", "direction without cost_budget_usd should not be paused on cost"


@pytest.mark.asyncio
async def test_process_lifecycle_flags_retirement_consideration_for_high_cost_per_merge(db_session, mocker):
    """Expensive directions are flagged when cost/merge exceeds project median * multiplier."""
    now = datetime.utcnow()
    expensive_direction = {
        "id": "d-expensive",
        "text": "Expensive direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [{
            "timestamp": now.isoformat(),
            "tasks_created": 2,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": 1,
            "estimated_cost_usd": 10.0,
        }],
    }
    project = Project(
        name="Retirement Flag Test",
        user_id="u-retire",
        autonomous_enabled=True,
        directions=[
            {
                "id": "d-cheap-1",
                "text": "Cheap 1",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [{
                    "timestamp": now.isoformat(),
                    "tasks_created": 2,
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "prs_merged": 1,
                    "estimated_cost_usd": 1.0,
                }],
            },
            {
                "id": "d-cheap-2",
                "text": "Cheap 2",
                "status": "active",
                "priority": "medium",
                "paths": [],
                "added_at": "2026-01-01T00:00:00",
                "research_history": [{
                    "timestamp": now.isoformat(),
                    "tasks_created": 2,
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "prs_merged": 1,
                    "estimated_cost_usd": 2.0,
                }],
            },
            expensive_direction,
        ],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {"direction_cost_per_merge_window": 6, "direction_cost_per_merge_multiplier": 2.0},
        clear=False,
    )

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=0.0,
        busy_direction_ids=set(), now=now,
    )
    await db_session.refresh(project)

    by_id = {d["id"]: d for d in project.directions}
    assert by_id["d-expensive"].get("retirement_consideration_needed") is True
    assert by_id["d-expensive"].get("retirement_consideration_evidence", {}).get("threshold_usd") == 4.0
    assert "retirement_consideration_needed" not in by_id["d-cheap-1"]
    assert "retirement_consideration_needed" not in by_id["d-cheap-2"]


@pytest.mark.asyncio
async def test_process_lifecycle_uses_default_cpm_config_when_runtime_values_invalid(db_session, mocker):
    """Invalid runtime config values should not crash lifecycle processing."""
    direction = {
        "id": "d-invalid-cpm-config",
        "text": "Config fallback direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": _cost_history(1.0, 1.0),
    }
    project = await _make_cost_project(db_session, direction)
    mocker.patch.dict(
        "app.direction_lifecycle._runtime_config",
        {
            "direction_cost_per_merge_window": "not-an-int",
            "direction_cost_per_merge_multiplier": "not-a-float",
        },
        clear=False,
    )

    await _process_direction_lifecycle(
        project, db_session, cooldown_minutes=0.0,
        busy_direction_ids=set(), now=datetime.utcnow(),
    )
    await db_session.refresh(project)

    assert project.directions[0]["status"] == "active"
