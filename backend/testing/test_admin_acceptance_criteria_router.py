"""Tests for the admin acceptance-criteria endpoints.

Exercises:
  GET    /api/admin/projects/{project_id}/acceptance-criteria
  POST   /api/admin/projects/{project_id}/acceptance-criteria
  DELETE /api/admin/projects/{project_id}/acceptance-criteria/{criterion_key}
  POST   /api/admin/projects/{project_id}/acceptance-criteria/link-direction

Role-enforcement, happy path, 400/404/409 branches.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app import acceptance_criteria as ac
from app.models import Direction, Project, ProjectMember, ReleaseVersion

TEST_USER_ID = "test-user-123"

_BASE = "/api/admin"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _make_project_member(session, role: str = "owner") -> Project:
    project = Project(name="AC-Router-Test", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role=role))
    await session.commit()
    return project


async def _make_version(session, project: Project, status: str = "open") -> ReleaseVersion:
    version = ReleaseVersion(project_id=project.id, version_number=1, status=status)
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def _make_direction(session, project: Project, key: str = "d-test1") -> Direction:
    direction = Direction(
        project_id=project.id,
        direction_key=key,
        data={"id": key, "text": "Test direction", "status": "active"},
        status="active",
    )
    session.add(direction)
    await session.commit()
    await session.refresh(direction)
    return direction


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/acceptance-criteria
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_criteria_no_open_version(client: AsyncClient, db_session):
    """Returns empty list when project has no open release version."""
    project = await _make_project_member(db_session)

    resp = await client.get(f"{_BASE}/projects/{project.id}/acceptance-criteria")
    assert resp.status_code == 200
    data = resp.json()
    assert data["release_version_id"] is None
    assert data["criteria"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_criteria_with_open_version(client: AsyncClient, db_session):
    """Returns criteria summary when an open version exists."""
    project = await _make_project_member(db_session)
    version = await _make_version(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="All tests pass",
        created_by="strategist",
    )

    resp = await client.get(f"{_BASE}/projects/{project.id}/acceptance-criteria")
    assert resp.status_code == 200
    data = resp.json()
    assert data["release_version_id"] == str(version.id)
    assert data["total"] == 1
    assert data["covered"] == 0
    assert data["uncovered"] == 1
    assert len(data["criteria"]) == 1
    assert data["criteria"][0]["criterion_key"] == criterion.criterion_key


@pytest.mark.asyncio
async def test_list_criteria_requires_viewer_role(client: AsyncClient, db_session):
    """Project non-member receives 404 (project appears not found)."""
    other_project = Project(name="Other", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp = await client.get(f"{_BASE}/projects/{other_project.id}/acceptance-criteria")
    assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# POST /api/admin/projects/{project_id}/acceptance-criteria (create)
# DELETE /api/admin/projects/{project_id}/acceptance-criteria/{key} (delete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_delete_acceptance_criterion(client: AsyncClient, db_session):
    """Creates and deletes criteria; exercises cap, de-dup, role, and open-version guards."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)

    # --- Create: happy path ---
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "All tests must pass"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["criterion_key"].startswith("c-")
    assert data["text"] == "All tests must pass"
    assert data["release_version_id"] == str(version.id)
    key1 = data["criterion_key"]

    # Create a second distinct criterion.
    resp2 = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "Coverage must be >= 80%"},
    )
    assert resp2.status_code == 200
    key2 = resp2.json()["criterion_key"]
    assert key2 != key1

    # --- link-direction de-dup: duplicate key in request stored only once ---
    direction = await _make_direction(db_session, project)
    resp_link = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": direction.direction_key, "criterion_keys": [key1, key1, key2]},
    )
    assert resp_link.status_code == 200
    linked_keys = resp_link.json()["criterion_keys"]
    assert linked_keys.count(key1) == 1, "duplicate key must be de-duped"
    assert key2 in linked_keys

    # --- Delete: happy path ---
    resp_del = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{key1}",
    )
    assert resp_del.status_code == 200
    assert resp_del.json()["status"] == "deleted"
    assert resp_del.json()["criterion_key"] == key1

    # --- Delete: non-existent key => 404 ---
    resp_del2 = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{key1}",
    )
    assert resp_del2.status_code == 404

    # --- Guard: empty text => 422 ---
    resp_empty = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "  "},
    )
    assert resp_empty.status_code == 422

    # --- Guard: no open version => 409 on create and delete ---
    version.status = "closed"
    db_session.add(version)
    await db_session.commit()

    resp_noversion = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "Should fail"},
    )
    assert resp_noversion.status_code == 409

    resp_del_noversion = await client.delete(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/{key2}",
    )
    assert resp_del_noversion.status_code == 409

    # --- Guard: role enforcement (non-member => 403/404) ---
    other_project = Project(name="OtherProj", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp_role = await client.post(
        f"{_BASE}/projects/{other_project.id}/acceptance-criteria",
        json={"text": "Blocked"},
    )
    assert resp_role.status_code in (403, 404)


@pytest.mark.asyncio
async def test_create_criterion_per_version_cap(client: AsyncClient, db_session):
    """Returns 409 once the per-version criterion cap is reached."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)

    # Fill up to the cap using the helper directly (avoid 50 HTTP calls).
    for i in range(ac.MAX_CRITERIA_PER_VERSION):
        await ac.create_criterion(
            db_session,
            project_id=project.id,
            release_version_id=version.id,
            text=f"Criterion {i}",
        )

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria",
        json={"text": "One too many"},
    )
    assert resp.status_code == 409, f"expected 409 cap enforcement, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST /api/admin/projects/{project_id}/acceptance-criteria/link-direction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_direction_happy_path(client: AsyncClient, db_session):
    """Links criterion keys to a direction and returns ok status."""
    project = await _make_project_member(db_session, role="member")
    version = await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="CI must pass",
    )

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": [criterion.criterion_key],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["direction_key"] == direction.direction_key
    assert criterion.criterion_key in data["criterion_keys"]

    # Verify the DB row was updated
    await db_session.refresh(direction)
    assert criterion.criterion_key in direction.criterion_keys


@pytest.mark.asyncio
async def test_link_direction_empty_criterion_keys(client: AsyncClient, db_session):
    """Empty criterion_keys list clears existing links without error."""
    project = await _make_project_member(db_session)
    await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": direction.direction_key, "criterion_keys": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_link_direction_409_no_open_version(client: AsyncClient, db_session):
    """Returns 409 when the project has no open release version."""
    project = await _make_project_member(db_session)
    direction = await _make_direction(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": direction.direction_key, "criterion_keys": []},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_link_direction_400_unknown_criterion_key(client: AsyncClient, db_session):
    """Returns 400 when any supplied criterion_key does not exist for the project."""
    project = await _make_project_member(db_session)
    await _make_version(db_session, project)
    direction = await _make_direction(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": ["c-nonexistent"],
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_link_direction_404_unknown_direction(client: AsyncClient, db_session):
    """Returns 404 when direction_key does not exist in the project."""
    project = await _make_project_member(db_session)
    await _make_version(db_session, project)

    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={"direction_key": "d-does-not-exist", "criterion_keys": []},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_link_direction_requires_member_role(client: AsyncClient, db_session):
    """Project non-member receives 403/404 on the link-direction endpoint."""
    other_project = Project(name="Other2", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()

    resp = await client.post(
        f"{_BASE}/projects/{other_project.id}/acceptance-criteria/link-direction",
        json={"direction_key": "d-x", "criterion_keys": []},
    )
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_link_direction_400_rejects_closed_version_criterion_key(
    client: AsyncClient, db_session
):
    """A criterion key from a closed/older version is rejected with 400.

    criterion_key is only unique within a release_version_id, so the validation
    must be scoped to the open version — not the whole project.
    """
    project = await _make_project_member(db_session, role="member")

    # Create a closed version with a criterion (version_number=1 so it sorts first).
    closed_version = ReleaseVersion(
        project_id=project.id, version_number=1, status="closed"
    )
    db_session.add(closed_version)
    await db_session.commit()
    await db_session.refresh(closed_version)
    closed_criterion = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=closed_version.id,
        text="Old closed criterion",
    )

    # Create the open version (version_number=2, no criteria yet).
    open_version = ReleaseVersion(
        project_id=project.id, version_number=2, status="open"
    )
    db_session.add(open_version)
    await db_session.commit()
    direction = await _make_direction(db_session, project)

    # Supplying the closed-version criterion key must be rejected.
    resp = await client.post(
        f"{_BASE}/projects/{project.id}/acceptance-criteria/link-direction",
        json={
            "direction_key": direction.direction_key,
            "criterion_keys": [closed_criterion.criterion_key],
        },
    )
    assert resp.status_code == 400, (
        "criterion key from a closed version must be rejected with 400; "
        f"got {resp.status_code}: {resp.text}"
    )
