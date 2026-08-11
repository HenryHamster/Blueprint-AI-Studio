"""Deterministic pagination coverage for the canonical admin task list."""

import uuid
from datetime import datetime, timezone

import pytest

from app.models import Project, Task, TaskStatus
from testing.factories import make_membership


@pytest.mark.asyncio
async def test_task_list_tied_timestamps_page_by_uuid_desc(client, db_session):
    project = Project(name="Tied task pagination", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        make_membership(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    tied_at = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    lower_id = uuid.UUID("00000000-0000-0000-0000-000000000101")
    higher_id = uuid.UUID("00000000-0000-0000-0000-000000000102")
    for task_id in (lower_id, higher_id):
        db_session.add(
            Task(
                id=task_id,
                project_id=project.id,
                title=f"Task {task_id}",
                description="pagination fixture",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create"}],
                created_at=tied_at,
            )
        )
    await db_session.commit()

    pages = []
    for offset in (0, 1):
        response = await client.get(
            "/api/admin/tasks",
            params={"project_id": str(project.id), "limit": 1, "offset": offset},
        )
        assert response.status_code == 200, response.text
        pages.append(response.json()["tasks"][0]["id"])

    assert pages == [str(higher_id), str(lower_id)]
    assert len(set(pages)) == 2
