"""Tests for GET /api/admin/projects/{project_id}/directions/{direction_key}/events.

Covers:
  - returns events for an existing direction (live row + event trail)
  - returns events for a pruned direction (no live row, only event trail)
  - 404 when direction has never existed
  - pagination via limit/offset
  - events ordered newest first
  - event fields present in response
"""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.models import Direction, DirectionEvent, Project, ProjectMember
from app.time_utils import _utcnow

TEST_USER_ID = "test-user-123"
DIRECTION_KEY = "d-lifecycle01"
PRUNED_KEY = "d-pruned01"
MISSING_KEY = "d-never-existed"


# ── helpers ───────────────────────────────────────────────────────────────────

async def _make_project(session) -> Project:
    project = Project(name="events-test-project", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="viewer"))
    await session.commit()
    return project


async def _make_direction_row(session, project_id, direction_key=DIRECTION_KEY) -> Direction:
    d = Direction(
        project_id=project_id,
        direction_key=direction_key,
        status="expired",
        data={"id": direction_key, "status": "expired"},
    )
    session.add(d)
    await session.commit()
    return d


async def _make_events(session, project_id) -> None:
    now = _utcnow()
    events = [
        DirectionEvent(
            id=uuid.uuid4(),
            project_id=project_id,
            direction_key=DIRECTION_KEY,
            event_kind="created",
            prior_status=None,
            new_status="active",
            recorded_at=now - timedelta(minutes=10),
        ),
        DirectionEvent(
            id=uuid.uuid4(),
            project_id=project_id,
            direction_key=DIRECTION_KEY,
            event_kind="status_changed",
            prior_status="active",
            new_status="expired",
            reason="retired by strategic review",
            recorded_at=now - timedelta(minutes=5),
        ),
        DirectionEvent(
            id=uuid.uuid4(),
            project_id=project_id,
            direction_key=PRUNED_KEY,
            event_kind="created",
            prior_status=None,
            new_status="active",
            recorded_at=now - timedelta(minutes=3),
        ),
        DirectionEvent(
            id=uuid.uuid4(),
            project_id=project_id,
            direction_key=PRUNED_KEY,
            event_kind="pruned",
            prior_status="active",
            new_status=None,
            recorded_at=now - timedelta(minutes=1),
        ),
    ]
    for e in events:
        session.add(e)
    await session.commit()


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_events_for_live_direction(client: AsyncClient, db_session):
    project = await _make_project(db_session)
    await _make_direction_row(db_session, project.id)
    await _make_events(db_session, project.id)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/directions/{DIRECTION_KEY}/events"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["direction_key"] == DIRECTION_KEY
    assert data["total"] == 2
    assert len(data["events"]) == 2
    # newest first
    assert data["events"][0]["event_kind"] == "status_changed"
    assert data["events"][1]["event_kind"] == "created"


@pytest.mark.asyncio
async def test_returns_events_for_pruned_direction(client: AsyncClient, db_session):
    """Event trail is queryable even after the direction row is deleted."""
    project = await _make_project(db_session)
    await _make_events(db_session, project.id)
    # PRUNED_KEY has no Direction table row — only events

    resp = await client.get(
        f"/api/admin/projects/{project.id}/directions/{PRUNED_KEY}/events"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["events"][0]["event_kind"] == "pruned"


@pytest.mark.asyncio
async def test_404_for_missing_direction(client: AsyncClient, db_session):
    project = await _make_project(db_session)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/directions/{MISSING_KEY}/events"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pagination_limit(client: AsyncClient, db_session):
    project = await _make_project(db_session)
    await _make_direction_row(db_session, project.id)
    await _make_events(db_session, project.id)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/directions/{DIRECTION_KEY}/events",
        params={"limit": 1, "offset": 0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["events"]) == 1  # limited to 1


@pytest.mark.asyncio
async def test_event_fields_present(client: AsyncClient, db_session):
    project = await _make_project(db_session)
    await _make_direction_row(db_session, project.id)
    await _make_events(db_session, project.id)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/directions/{DIRECTION_KEY}/events"
    )
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    required_fields = {
        "id", "direction_key", "event_kind", "prior_status",
        "new_status", "actor", "reason", "snapshot_data", "recorded_at",
    }
    assert required_fields.issubset(event.keys())
    # newest event is status_changed with reason
    assert event["reason"] == "retired by strategic review"
    assert event["prior_status"] == "active"
    assert event["new_status"] == "expired"
