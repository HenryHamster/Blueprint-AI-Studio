"""Deterministic pagination and freshness coverage for the canonical admin task list."""

import uuid
from datetime import datetime, timedelta, timezone

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


# Every datetime-valued key the task-list row emits. A caller that measures
# task age reads these; every one of them has to say which zone it is in.
_TASK_LIST_TIMESTAMP_FIELDS = (
    "created_at",
    "updated_at",
    "started_at",
    "last_heartbeat",
    "last_tool_use_at",
    "blocked_at",
    "recheck_after",
    "parked_at",
)


async def _seed_freshness_task(db_session, *, created, moved):
    """One task whose created_at and updated_at are deliberately far apart."""

    project = Project(name="Task list freshness", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        make_membership(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    task_id = uuid.UUID("00000000-0000-0000-0000-000000000201")
    db_session.add(
        Task(
            id=task_id,
            project_id=project.id,
            title="Freshness fixture",
            description="freshness fixture",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "create"}],
            created_at=created,
            updated_at=moved,
            started_at=created,
            last_heartbeat=moved,
        )
    )
    await db_session.commit()
    return project, task_id


@pytest.mark.asyncio
async def test_task_list_reports_when_each_task_last_moved(client, db_session):
    """``updated_at`` was loaded by the query and then dropped on the floor.

    ``_TASK_LIST_COLS`` has always selected ``Task.updated_at``, but the row
    builder never wrote it to the response. A task that moved five seconds ago
    and a task frozen for six hours were therefore indistinguishable to every
    consumer of this listing -- ``blueprint tasks list --json`` included --
    which is exactly the question asked when diagnosing a wedge.

    ``created_at`` and ``updated_at`` are seeded six and a half hours apart so
    a serializer that aliases one to the other cannot pass this.
    """

    created = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    moved = datetime(2026, 8, 10, 18, 30, 0, tzinfo=timezone.utc)
    project, task_id = await _seed_freshness_task(
        db_session, created=created, moved=moved
    )

    response = await client.get(
        "/api/admin/tasks", params={"project_id": str(project.id)}
    )
    assert response.status_code == 200, response.text
    rows = response.json()["tasks"]
    assert [row["id"] for row in rows] == [str(task_id)]
    row = rows[0]

    assert "updated_at" in row, "task-list rows must carry updated_at"
    assert row["updated_at"] is not None
    assert datetime.fromisoformat(row["updated_at"]) == moved
    assert datetime.fromisoformat(row["created_at"]) == created
    assert row["updated_at"] != row["created_at"]


@pytest.mark.asyncio
async def test_task_list_timestamps_carry_an_explicit_utc_offset(
    client, db_session
):
    """Naive ISO strings are a silent wrong answer at both consumers.

    The row builder called ``.isoformat()`` on the naive-UTC datetimes the
    ``NaiveUTCDateTime`` column type hands back, so the wire carried
    ``2026-08-10T12:00:00`` -- a string that states no zone. Subtracting that
    from an aware ``datetime.now(timezone.utc)`` raises ``TypeError``, and
    ``new Date()`` in a browser reads it as *local* time and shifts every
    displayed age. Neither failure announces itself.
    """

    created = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    moved = datetime(2026, 8, 10, 18, 30, 0, tzinfo=timezone.utc)
    project, _task_id = await _seed_freshness_task(
        db_session, created=created, moved=moved
    )

    response = await client.get(
        "/api/admin/tasks", params={"project_id": str(project.id)}
    )
    assert response.status_code == 200, response.text
    row = response.json()["tasks"][0]

    checked = 0
    for field in _TASK_LIST_TIMESTAMP_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None, f"{field} is naive: {value!r}"
        assert parsed.utcoffset() == timedelta(0), (
            f"{field} is not rendered in UTC: {value!r}"
        )
        # The operative consumer behaviour: age against an aware clock. A
        # naive string makes this raise TypeError.
        datetime.now(timezone.utc) - parsed
        checked += 1
    assert checked >= 4, f"expected populated timestamps to assert on, got {checked}"
