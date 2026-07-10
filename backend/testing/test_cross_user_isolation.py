"""Cross-user isolation integration tests.

Verifies that user_A's JWT is denied access to user_B's project resources
when user_A has zero ProjectMember rows for user_B's project, and vice-versa.

The _get_user_project helper in admin_helpers.py returns 404 when a
ProjectMember row is absent — these tests confirm that invariant holds for
the key resource endpoints (project detail, task detail, artifact download,
compute-job detail).
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.auth import get_current_user, get_current_user_optional, require_admin
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
)
from app import storage

USER_A = "user-a-111"
USER_B = "user-b-222"

_TEST_CSRF = "test-csrf-cross-user"


# ---------------------------------------------------------------------------
# Fixtures: two clients authenticated as different users
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_a(override_get_session):
    """HTTP client authenticated as user_A."""

    async def _auth_a():
        return {"user_id": USER_A, "email": "user-a@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user] = _auth_a
    app.dependency_overrides[get_current_user_optional] = _auth_a
    app.dependency_overrides[require_admin] = _auth_a
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-CSRF-Token": _TEST_CSRF},
        cookies={"csrf_token": _TEST_CSRF},
    ) as ac:
        yield ac
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_user_optional, None)
    app.dependency_overrides.pop(require_admin, None)


@pytest_asyncio.fixture
async def client_b(override_get_session):
    """HTTP client authenticated as user_B (non-admin role)."""

    async def _auth_b():
        return {"user_id": USER_B, "email": "user-b@example.com", "role": "member"}

    app.dependency_overrides[get_current_user] = _auth_b
    app.dependency_overrides[get_current_user_optional] = _auth_b
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-CSRF-Token": _TEST_CSRF},
        cookies={"csrf_token": _TEST_CSRF},
    ) as ac:
        yield ac
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_user_optional, None)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _make_project(session, user_id, name="Project"):
    project = Project(name=name, user_id=user_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _add_member(session, project_id, user_id, role="member"):
    session.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))
    await session.commit()


async def _make_task(session, project):
    task = Task(
        project_id=project.id,
        title="cross-user task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _make_compute_job(session, project, task):
    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
    )
    job.result_reference = (
        f"gs://{storage.ARTIFACT_BUCKET_NAME}/compute-jobs/{job.id}/artifacts/bundle.tar.gz"
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_user_project_access_denied(client_a: AsyncClient, db_session):
    """user_A is denied access to user_B's project (no membership row)."""
    project_b = await _make_project(db_session, user_id=USER_B, name="B-project")
    # Intentionally do NOT add user_A as a member.

    resp = await client_a.get(f"/api/admin/projects/{project_b.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_user_task_access_denied(client_a: AsyncClient, db_session):
    """user_A is denied access to a task in user_B's project."""
    project_b = await _make_project(db_session, user_id=USER_B, name="B-tasks")
    task = await _make_task(db_session, project_b)

    resp = await client_a.get(f"/api/admin/tasks/{task.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_user_artifact_download_denied(client_a: AsyncClient, db_session, mocker):
    """user_A cannot download an artifact belonging to user_B's project."""
    sign = mocker.patch("app.storage.storage.Client")
    project_b = await _make_project(db_session, user_id=USER_B, name="B-artifacts")
    task = await _make_task(db_session, project_b)
    uri = f"gs://{storage.ARTIFACT_BUCKET_NAME}/tasks/{task.id}/out.json"

    resp = await client_a.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project_b.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()


@pytest.mark.asyncio
async def test_cross_user_same_project_member_allowed(client_a: AsyncClient, db_session):
    """user_A CAN access user_B's project when explicitly added as a member."""
    project_b = await _make_project(db_session, user_id=USER_B, name="B-shared")
    await _add_member(db_session, project_b.id, USER_A)

    resp = await client_a.get(f"/api/admin/projects/{project_b.id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_cross_user_compute_job_artifact_download_denied(
    client_a: AsyncClient, db_session, mocker
):
    """user_A cannot download a compute-job artifact from user_B's project."""
    sign = mocker.patch("app.storage.storage.Client")
    project_b = await _make_project(db_session, user_id=USER_B, name="B-jobs")
    task = await _make_task(db_session, project_b)
    job = await _make_compute_job(db_session, project_b, task)

    resp = await client_a.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": job.result_reference, "project_id": str(project_b.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()


# ---------------------------------------------------------------------------
# Bidirectional isolation: user_B (non-admin) cannot access user_A's resources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_user_b_cannot_access_a_project(
    client_b: AsyncClient, db_session
):
    """Non-admin user_B is denied access to user_A's project (no membership row)."""
    project_a = await _make_project(db_session, user_id=USER_A, name="A-project")

    resp = await client_b.get(f"/api/admin/projects/{project_a.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_user_b_cannot_access_a_task(
    client_b: AsyncClient, db_session
):
    """Non-admin user_B is denied access to a task in user_A's project."""
    project_a = await _make_project(db_session, user_id=USER_A, name="A-tasks")
    task = await _make_task(db_session, project_a)

    resp = await client_b.get(f"/api/admin/tasks/{task.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_user_b_cannot_download_a_artifact(
    client_b: AsyncClient, db_session, mocker
):
    """Non-admin user_B cannot download an artifact from user_A's project, even with a valid UUID."""
    sign = mocker.patch("app.storage.storage.Client")
    project_a = await _make_project(db_session, user_id=USER_A, name="A-artifacts")
    task = await _make_task(db_session, project_a)
    uri = f"gs://{storage.ARTIFACT_BUCKET_NAME}/tasks/{task.id}/out.json"

    resp = await client_b.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project_a.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()


@pytest.mark.asyncio
async def test_cross_user_compute_job_detail_denied(
    client_a: AsyncClient, db_session
):
    """user_A cannot fetch compute-job detail for a job in user_B's project."""
    project_b = await _make_project(db_session, user_id=USER_B, name="B-cj-detail")
    task = await _make_task(db_session, project_b)
    job = await _make_compute_job(db_session, project_b, task)

    resp = await client_a.get(f"/api/admin/compute-jobs/{job.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_user_b_cannot_access_a_compute_job(
    client_b: AsyncClient, db_session
):
    """Non-admin user_B cannot fetch compute-job detail for a job in user_A's project."""
    project_a = await _make_project(db_session, user_id=USER_A, name="A-cj-detail")
    task = await _make_task(db_session, project_a)
    job = await _make_compute_job(db_session, project_a, task)

    resp = await client_b.get(f"/api/admin/compute-jobs/{job.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cross_user_artifact_wrong_project_id_denied(
    client_a: AsyncClient, db_session, mocker
):
    """Ownership check: user_A supplying project_B's ID for a task in project_A is denied.

    Guards the centralized verify_artifact_project_ownership check: even when
    the user has access to project_a, requesting the URI with a mismatched
    project_b ID must be rejected (404) without signing.
    """
    sign = mocker.patch("app.storage.storage.Client")
    project_a = await _make_project(db_session, user_id=USER_A, name="A-ownership")
    project_b = await _make_project(db_session, user_id=USER_B, name="B-ownership")
    # user_A is a member of project_a (owner) but not project_b
    from app.models import ProjectMember
    db_session.add(ProjectMember(project_id=project_a.id, user_id=USER_A, role="owner"))
    await db_session.commit()

    task = await _make_task(db_session, project_a)
    uri = f"gs://{storage.ARTIFACT_BUCKET_NAME}/tasks/{task.id}/out.json"

    # Request the artifact with project_b's ID — ownership mismatch must be rejected
    resp = await client_a.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project_b.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()
