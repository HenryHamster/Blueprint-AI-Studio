import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.direction_lifecycle import _detect_partial_remediation_success
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
