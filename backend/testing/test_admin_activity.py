"""Tests for /api/admin/activity PR impact annotations on TASK_COMPLETED events."""
import pytest
from httpx import AsyncClient

from app.models import Project, ProjectMember, Task, TaskStatus


TEST_USER_ID = "test-user-123"


async def _make_project(session, name="Activity Test Project"):
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


@pytest.mark.asyncio
async def test_completed_task_includes_pr_impact_annotations(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    many_files = [f"backend/app/mod_{i}.py" for i in range(12)]
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        title="Feat: do the thing",
        context={
            "_pr_changed_files": many_files,
            "scope": "backend",
            "direction_id": "dir-abc-123-xyz",
            "review_loop": {"iteration": 3},
        },
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    events = resp.json()["events"]
    completed = [e for e in events if e["type"] == "TASK_COMPLETED"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["changed_files"] is not None
    assert len(ev["changed_files"]) == 10
    assert ev["changed_files"] == many_files[:10]
    assert ev["scope"] == "backend"
    assert ev["direction_id"] == "dir-abc-123-xyz"
    assert ev["review_iterations"] == 3


@pytest.mark.asyncio
async def test_completed_task_missing_context_fields_are_none(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        title="Bare completed task",
        context={},
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    completed = [e for e in resp.json()["events"] if e["type"] == "TASK_COMPLETED"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["changed_files"] is None
    assert ev["scope"] is None
    assert ev["direction_id"] is None
    assert ev["review_iterations"] is None
