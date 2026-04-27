import pytest

from app.models import Project, Task, TaskStatus
from app.spawn_calibration import _assess_project_spawn_health


@pytest.mark.asyncio
async def test_over_budget_active_project_tasks_degrade_spawn_health(db_session, mocker, monkeypatch):
    """D14: queued/in-flight backlog blocks additional implementation dispatch."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    mocker.patch("app.compute.any_zone_available", return_value=True)
    project = Project(
        name="D14 backlog",
        user_id="u-d14",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.QUEUED,
    ]
    for idx, status in enumerate(statuses):
        db_session.add(Task(
            project_id=project.id,
            title=f"active task {idx}",
            payloads=[{"type": "create", "prompt": "work"}],
            status=status,
            created_by="ORCHESTRATOR",
        ))
    await db_session.commit()

    degraded, reason = await _assess_project_spawn_health(
        db_session,
        project.id,
        design_cap=2,
    )

    assert degraded is True
    assert reason is not None
    assert "active task backlog" in reason
