"""Integration tests for /api/admin/digest and /api/admin/activity endpoints."""
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import TaskStatus
from testing.factories import make_project, make_task, _utcnow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def unauthenticated_client(override_get_session):
    """Client with no auth override — useful for testing 401/403 responses."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# /api/admin/digest tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_empty_window(client: AsyncClient):
    """No tasks in the DB → all counts are 0 and lists are empty."""
    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()
    assert data["window_hours"] == 24
    assert data["completed_count"] == 0
    assert data["failed_count"] == 0
    assert data["pending_approval_count"] == 0
    assert data["research_cycles"] == 0
    assert data["prs_opened"] == 0
    assert data["needs_attention"] == []
    assert data["recent_failures"] == []
    assert data["recent_completions"] == []


@pytest.mark.asyncio
async def test_digest_with_completed_tasks(client: AsyncClient, db_session):
    """Completed tasks inside the window appear in recent_completions with correct count."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task1 = make_task(
        project_id=project.id,
        title="Completed Task 1",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create"}],
    )
    task2 = make_task(
        project_id=project.id,
        title="Completed Task 2",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "research"}],
    )
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 2
    assert data["failed_count"] == 0
    assert data["research_cycles"] == 1  # only the research-type task

    titles = {c["title"] for c in data["recent_completions"]}
    assert "Completed Task 1" in titles
    assert "Completed Task 2" in titles


@pytest.mark.asyncio
async def test_digest_with_failed_tasks(client: AsyncClient, db_session):
    """Failed tasks appear in both needs_attention (reason='failed') and recent_failures."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["failed_count"] == 1
    assert data["completed_count"] == 0

    assert len(data["needs_attention"]) == 1
    attention = data["needs_attention"][0]
    assert attention["task_id"] == str(task.id)
    assert attention["title"] == "Failed Task"
    assert attention["reason"] == "failed"

    assert len(data["recent_failures"]) == 1
    failure = data["recent_failures"][0]
    assert failure["task_id"] == str(task.id)
    assert failure["title"] == "Failed Task"
    assert "failed_at" in failure


@pytest.mark.asyncio
async def test_digest_with_pending_approval(client: AsyncClient, db_session):
    """Tasks requiring approval (non-terminal) appear in needs_attention with reason='approval_required'."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="Approval Task",
        status=TaskStatus.IN_PROGRESS,
        requires_approval=True,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    response = await client.get("/api/admin/digest")
    assert response.status_code == 200
    data = response.json()

    assert data["pending_approval_count"] == 1

    attention_reasons = {a["reason"] for a in data["needs_attention"]}
    assert "approval_required" in attention_reasons

    approval_items = [a for a in data["needs_attention"] if a["reason"] == "approval_required"]
    assert len(approval_items) == 1
    assert approval_items[0]["task_id"] == str(task.id)
    assert approval_items[0]["title"] == "Approval Task"


@pytest.mark.asyncio
async def test_digest_time_window_filtering(client: AsyncClient, db_session):
    """Tasks with created_at outside the requested hours window are excluded."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task inside the 24-hour window
    inside_task = make_task(
        project_id=project.id,
        title="Inside Window",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=12),
    )
    # Task outside the 24-hour window
    outside_task = make_task(
        project_id=project.id,
        title="Outside Window",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=25),
    )
    db_session.add(inside_task)
    db_session.add(outside_task)
    await db_session.commit()

    response = await client.get("/api/admin/digest?hours=24")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Inside Window" in titles
    assert "Outside Window" not in titles


@pytest.mark.asyncio
async def test_digest_project_filter(client: AsyncClient, db_session):
    """project_id query param filters tasks to only the specified project."""
    project_a = make_project(name="Project A")
    project_b = make_project(name="Project B")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = make_task(
        project_id=project_a.id,
        title="Task A",
        status=TaskStatus.COMPLETED,
    )
    task_b = make_task(
        project_id=project_b.id,
        title="Task B",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()

    response = await client.get(f"/api/admin/digest?project_id={project_a.id}")
    assert response.status_code == 200
    data = response.json()

    assert data["completed_count"] == 1
    titles = [c["title"] for c in data["recent_completions"]]
    assert "Task A" in titles
    assert "Task B" not in titles


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_digest_requires_auth(unauthenticated_client: AsyncClient):
    """Requests without a valid token are rejected with 401."""
    response = await unauthenticated_client.get(
        "/api/admin/digest",
        headers={"Authorization": "Bearer bogus-token"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# /api/admin/activity tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_basic(client: AsyncClient, db_session):
    """Completed and failed tasks generate TASK_COMPLETED and TASK_FAILED events."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    completed = make_task(
        project_id=project.id,
        title="Completed Task",
        status=TaskStatus.COMPLETED,
    )
    failed = make_task(
        project_id=project.id,
        title="Failed Task",
        status=TaskStatus.FAILED,
    )
    db_session.add(completed)
    db_session.add(failed)
    await db_session.commit()

    response = await client.get("/api/admin/activity")
    assert response.status_code == 200
    data = response.json()

    assert "events" in data
    assert "total" in data
    assert data["total"] >= 2

    event_types = {e["type"] for e in data["events"]}
    assert "TASK_COMPLETED" in event_types
    assert "TASK_FAILED" in event_types


@pytest.mark.asyncio
async def test_activity_since_filter(client: AsyncClient, db_session):
    """The since parameter filters out events before the cutoff timestamp."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_task = make_task(
        project_id=project.id,
        title="Old Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(days=10),
    )
    new_task = make_task(
        project_id=project.id,
        title="New Task",
        status=TaskStatus.COMPLETED,
        created_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(old_task)
    db_session.add(new_task)
    await db_session.commit()
    await db_session.refresh(old_task)
    await db_session.refresh(new_task)

    # Use a cutoff that excludes the old task
    since = (_utcnow() - timedelta(days=5)).isoformat()
    response = await client.get(f"/api/admin/activity?since={since}")
    assert response.status_code == 200
    data = response.json()

    task_ids = [e["task_id"] for e in data["events"] if e.get("task_id")]
    assert str(new_task.id) in task_ids
    assert str(old_task.id) not in task_ids


@pytest.mark.asyncio
async def test_activity_limit(client: AsyncClient, db_session):
    """The limit parameter caps the number of events returned."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(5):
        task = make_task(
            project_id=project.id,
            title=f"Task {i}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(task)
    await db_session.commit()

    response = await client.get("/api/admin/activity?limit=2")
    assert response.status_code == 200
    data = response.json()

    assert len(data["events"]) <= 2
    # total reflects all events found before limiting
    assert data["total"] >= 5


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_activity_requires_auth(unauthenticated_client: AsyncClient):
    """Requests without a valid token are rejected with 401."""
    response = await unauthenticated_client.get(
        "/api/admin/activity",
        headers={"Authorization": "Bearer bogus-token"},
    )
    assert response.status_code == 401
