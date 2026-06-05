"""Tests for the operator compute-jobs admin endpoints (list + kill).

Project scoping is enforced by ``_get_user_project_with_role`` (viewer to list,
member to kill) — the test user is a global admin via the conftest auth
override, but the endpoints still require an explicit ProjectMember row, so each
test seeds the role it exercises.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.models import (
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
)

TEST_USER_ID = "test-user-123"


async def _make_project(session, name="Compute Project"):
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _add_member(session, project_id, role):
    session.add(ProjectMember(project_id=project_id, user_id=TEST_USER_ID, role=role))
    await session.commit()


async def _make_job(session, project, *, status=ComputeJobStatus.RUNNING, title="compute_experiment task"):
    task = Task(
        project_id=project.id,
        title=title,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=status,
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        accrued_cost_usd=0.5,
        result_reference="https://storage.example.com/jobs/job-1/result.json",
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return task, job


@pytest.mark.asyncio
async def test_list_compute_jobs_viewer_ok(client: AsyncClient, db_session):
    project = await _make_project(db_session)
    await _add_member(db_session, project.id, "viewer")
    _task, job = await _make_job(db_session, project)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == str(job.id)
    assert jobs[0]["task_title"] == "compute_experiment task"
    assert jobs[0]["task_type"] == "compute_experiment"
    assert jobs[0]["result_reference"] == "https://storage.example.com/jobs/job-1/result.json"


@pytest.mark.asyncio
async def test_list_compute_jobs_non_member_404(client: AsyncClient, db_session):
    project = await _make_project(db_session, name="No Member")
    await _make_job(db_session, project)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kill_compute_job_member_ok(client: AsyncClient, db_session):
    project = await _make_project(db_session, name="Killable")
    await _add_member(db_session, project.id, "member")
    _task, job = await _make_job(db_session, project)

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 200
    body = resp.json()["compute_job"]
    assert body["status"] == ComputeJobStatus.CANCELLED.value
    assert body["outcome"] == ComputeJobOutcome.CANCELLED.value
    assert "operator" in (body["terminal_reason"] or "")


@pytest.mark.asyncio
async def test_kill_compute_job_viewer_forbidden(client: AsyncClient, db_session):
    project = await _make_project(db_session, name="Viewer Only")
    await _add_member(db_session, project.id, "viewer")
    _task, job = await _make_job(db_session, project)

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 403
    # Job remains active — the forbidden kill must not have mutated it.
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING


@pytest.mark.asyncio
async def test_kill_compute_job_missing_404(client: AsyncClient, db_session):
    resp = await client.post(f"/api/admin/compute-jobs/{uuid.uuid4()}/kill")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kill_already_terminal_returns_409(client: AsyncClient, db_session):
    project = await _make_project(db_session, name="Already Done")
    await _add_member(db_session, project.id, "member")
    _task, job = await _make_job(db_session, project, status=ComputeJobStatus.SUCCEEDED)

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 409
