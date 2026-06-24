"""Tests for the operator compute-jobs admin endpoints (list + kill).

Project scoping is enforced by ``_get_user_project_with_role`` (viewer to list,
member to kill) — the test user is a global admin via the conftest auth
override, but the endpoints still require an explicit ProjectMember row, so each
test seeds the role it exercises.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    KnowledgeDocument,
    Project,
    ProjectMember,
    SourceKind,
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


async def _make_job(
    session,
    project,
    *,
    status=ComputeJobStatus.RUNNING,
    title="compute_experiment task",
    with_artifact=False,
    polling_metadata=None,
):
    task = Task(
        project_id=project.id,
        title=title,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    artifact = None
    if with_artifact:
        artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.COMPUTE_RESULT,
            storage_uri="gs://bucket/jobs/job-1/result.parquet",
            logical_key="compute-result-job-1",
        )
        session.add(artifact)
        await session.commit()
        await session.refresh(artifact)
    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=status,
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        accrued_cost_usd=0.5,
        result_reference="https://storage.example.com/jobs/job-1/result.json",
        artifact_id=artifact.id if artifact else None,
        polling_metadata=polling_metadata or {},
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return task, job, artifact


@pytest.mark.asyncio
async def test_list_compute_jobs_viewer_ok(client: AsyncClient, db_session):
    project = await _make_project(db_session)
    await _add_member(db_session, project.id, "viewer")
    _task, job, _artifact = await _make_job(db_session, project)
    job.polling_metadata = {
        "zone": "us-central1-a",
        "operation": "op-123",
        "command": "python launch.py --secret",
    }
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == str(job.id)
    assert jobs[0]["task_title"] == "compute_experiment task"
    assert jobs[0]["task_type"] == "compute_experiment"
    assert (
        jobs[0]["result_reference"]
        == "https://storage.example.com/jobs/job-1/result.json"
    )
    # A job with only a raw result_reference and no linked Artifact must expose
    # the descriptor explicitly as null so operators can tell the two apart.
    assert jobs[0]["artifact"] is None
    assert "command" not in jobs[0]["polling_metadata"]


@pytest.mark.asyncio
async def test_list_compute_jobs_paginates_newest_first(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session, name="Paged")
    await _add_member(db_session, project.id, "viewer")
    _task_1, job_1, _artifact_1 = await _make_job(db_session, project, title="job 1")
    _task_2, job_2, _artifact_2 = await _make_job(db_session, project, title="job 2")
    _task_3, _job_3, _artifact_3 = await _make_job(db_session, project, title="job 3")

    resp = await client.get(
        f"/api/admin/projects/{project.id}/compute-jobs?limit=2&offset=1"
    )
    assert resp.status_code == 200
    body = resp.json()
    jobs = body["compute_jobs"]
    assert body["has_more"] is False
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [job["id"] for job in jobs] == [str(job_2.id), str(job_1.id)]
    assert [job["task_title"] for job in jobs] == ["job 2", "job 1"]


@pytest.mark.asyncio
async def test_linked_compute_result_artifact_descriptor_serialized(
    client: AsyncClient, db_session
):
    """A job whose artifact_id resolves to a COMPUTE_RESULT Artifact exposes a
    structured `artifact` descriptor carrying the artifact's identity and
    download affordance (id/type/storage_uri/logical_key/created_at)."""
    project = await _make_project(db_session, name="Linked Artifact")
    await _add_member(db_session, project.id, "viewer")
    _task, job, artifact = await _make_job(db_session, project, with_artifact=True)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == str(job.id)
    assert jobs[0]["artifact_id"] == str(artifact.id)

    descriptor = jobs[0]["artifact"]
    assert descriptor is not None
    assert descriptor["id"] == str(artifact.id)
    assert descriptor["artifact_type"] == ArtifactType.COMPUTE_RESULT.value
    assert descriptor["storage_uri"] == "gs://bucket/jobs/job-1/result.parquet"
    assert descriptor["logical_key"] == "compute-result-job-1"
    assert descriptor["created_at"] is not None


@pytest.mark.asyncio
async def test_list_compute_jobs_no_artifact_omits_descriptor(
    client: AsyncClient, db_session
):
    """A job carrying only a result_reference (no artifact_id) must serialize a
    null `artifact` descriptor — the distinction has to be explicit."""
    project = await _make_project(db_session, name="No Artifact")
    await _add_member(db_session, project.id, "viewer")
    _task, job, _artifact = await _make_job(db_session, project)
    assert job.artifact_id is None

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert jobs[0]["artifact"] is None
    assert jobs[0]["result_reference"] is not None


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
    _task, job, _artifact = await _make_job(db_session, project)

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 200
    body = resp.json()["compute_job"]
    assert body["status"] == ComputeJobStatus.CANCELLED.value
    assert body["outcome"] == ComputeJobOutcome.CANCELLED.value
    assert "operator" in (body["terminal_reason"] or "")


@pytest.mark.asyncio
async def test_kill_compute_job_backend_terminate_failure_returns_diagnostic(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session, name="Kill RPC Failure")
    await _add_member(db_session, project.id, "member")
    _task, job, _artifact = await _make_job(db_session, project)
    job.external_compute_id = "admin-kill-rpc-failure"
    db_session.add(job)
    await db_session.commit()

    class FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            assert reason == "operator"
            raise RuntimeError("terminate backend unavailable")

    with patch("app.compute_jobs.get_compute_backend", lambda _name: FakeBackend()):
        resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")

    assert resp.status_code == 200
    body = resp.json()["compute_job"]
    assert body["status"] == ComputeJobStatus.RUNNING.value
    assert body["outcome"] is None
    diagnostic = body["compute_rpc_diagnostic"]
    assert diagnostic["operation"] == "terminate"
    assert diagnostic["reason"] == "operator"
    assert "terminate backend unavailable" in diagnostic["error_message"]
    assert body["polling_metadata"]["compute_rpc_diagnostic"] == diagnostic


@pytest.mark.asyncio
async def test_kill_compute_job_viewer_returns_404(client: AsyncClient, db_session):
    """Viewers must not be able to confirm job existence via a 403 response (IDOR)."""
    project = await _make_project(db_session, name="Viewer Only")
    await _add_member(db_session, project.id, "viewer")
    _task, job, _artifact = await _make_job(db_session, project)

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    # Must be 404, not 403, so job existence is not leaked to callers without kill rights.
    assert resp.status_code == 404
    # Job remains active — the unauthorized kill must not have mutated it.
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING


@pytest.mark.asyncio
async def test_kill_cross_project_job_returns_404(client: AsyncClient, db_session):
    """A member of project A must get 404 (not 403) when killing project B's job.

    The response must be indistinguishable from a missing job to prevent IDOR.
    """
    project_a = await _make_project(db_session, name="Project A")
    await _add_member(db_session, project_a.id, "member")

    project_b = await _make_project(db_session, name="Project B")
    # Deliberately no membership in project_b for the test user.
    _task_b, job_b, _artifact_b = await _make_job(db_session, project_b)

    resp = await client.post(f"/api/admin/compute-jobs/{job_b.id}/kill")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kill_compute_job_missing_404(client: AsyncClient, db_session):
    resp = await client.post(f"/api/admin/compute-jobs/{uuid.uuid4()}/kill")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kill_already_terminal_returns_409(client: AsyncClient, db_session):
    project = await _make_project(db_session, name="Already Done")
    await _add_member(db_session, project.id, "member")
    _task, job, _artifact = await _make_job(
        db_session, project, status=ComputeJobStatus.SUCCEEDED
    )

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 409


async def _make_job_with_polling_metadata(session, project, polling_metadata):
    """Seed a job whose polling_metadata carries the given dict (for redaction tests)."""
    task = Task(
        project_id=project.id,
        title="compute_experiment task",
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
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        polling_metadata=polling_metadata,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return task, job


@pytest.mark.asyncio
async def test_list_compute_jobs_redacts_launch_secrets(
    client: AsyncClient, db_session
):
    """Operator list must not expose raw env values, command, or cwd, but must
    preserve operator-useful keys like live_release_evidence, result_metrics,
    and compute_rpc_diagnostic."""
    project = await _make_project(db_session, name="Redact List")
    await _add_member(db_session, project.id, "viewer")
    _task, _job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "command": ["python3", "run_experiment.py"],
            "cwd": "/workspace",
            "env": {
                "SECRET_TOKEN": "super-secret-value",
                "ANOTHER_VAR": "another-secret",
            },
            "live_release_evidence": {"version": "v8", "metrics": {"cpm": 1.23}},
            "result_metrics": {"accuracy": 0.95},
            "compute_rpc_diagnostic": {"operation": "poll", "error_message": "timeout"},
        },
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    pm = resp.json()["compute_jobs"][0]["polling_metadata"]

    # Secret-bearing fields must be absent.
    assert "command" not in pm
    assert "cwd" not in pm

    assert "env" not in pm

    # Operator-useful keys must survive redaction.
    assert pm["live_release_evidence"] == {"version": "v8", "metrics": {"cpm": 1.23}}
    assert pm["result_metrics"] == {"accuracy": 0.95}
    assert pm["compute_rpc_diagnostic"]["operation"] == "poll"


@pytest.mark.asyncio
async def test_kill_compute_job_redacts_launch_secrets(client: AsyncClient, db_session):
    """Kill response must apply the same polling_metadata redaction as the list."""
    project = await _make_project(db_session, name="Redact Kill")
    await _add_member(db_session, project.id, "member")
    _task, job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "command": ["python3", "run.py"],
            "cwd": "/workspace",
            "env": {"API_KEY": "top-secret"},
            "live_release_evidence": {"version": "v8"},
        },
    )

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 200
    pm = resp.json()["compute_job"]["polling_metadata"]

    assert "command" not in pm
    assert "cwd" not in pm
    assert "env" not in pm
    assert pm["live_release_evidence"] == {"version": "v8"}


@pytest.mark.asyncio
async def test_list_compute_jobs_strips_non_dict_env_payload(
    client: AsyncClient, db_session
):
    """Operator list must drop env entirely even if the payload shape is odd."""
    project = await _make_project(db_session, name="Non Dict Env")
    await _add_member(db_session, project.id, "viewer")
    _task, _job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "env": "PATH=/workspace/bin",
            "command": ["python3", "run.py"],
            "cwd": "/workspace",
        },
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    metadata = resp.json()["compute_jobs"][0]["polling_metadata"]
    assert "env" not in metadata
    assert "command" not in metadata
    assert "cwd" not in metadata


@pytest.mark.asyncio
async def test_kill_peer_terminal_returns_final_state(client: AsyncClient, db_session):
    """When a peer drives the job terminal during the lock-free RPC window,
    kill_compute_job returns None. The router must re-fetch and return the
    terminal job rather than blowing up with a 500."""
    project = await _make_project(db_session, name="Peer Terminal")
    await _add_member(db_session, project.id, "member")
    _task, job, _artifact = await _make_job(db_session, project)

    # Simulate the peer-terminal race: kill_compute_job returns None (peer won),
    # but the job in DB is already CANCELLED.
    job.status = ComputeJobStatus.CANCELLED
    job.outcome = ComputeJobOutcome.CANCELLED
    job.terminal_reason = "killed by peer"
    db_session.add(job)
    await db_session.commit()

    with patch(
        "app.routers.admin_compute_jobs.kill_compute_job",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")

    assert resp.status_code == 200
    body = resp.json()["compute_job"]
    assert body["status"] == ComputeJobStatus.CANCELLED.value
    assert body["outcome"] == ComputeJobOutcome.CANCELLED.value


_SENSITIVE_POLLING_METADATA = {
    "env": {"SECRET_KEY": "mysecret", "DATABASE_URL": "postgres://..."},
    "command": "python3 run_experiment.py --flag",
    "cwd": "/home/agent/workspace",
    "status_message": "running",
    "result_metrics": {"accuracy": 0.95},
}


@pytest.mark.asyncio
async def test_list_compute_jobs_strips_sensitive_polling_metadata(
    client: AsyncClient, db_session
):
    """List endpoint must not expose env, command, or cwd in polling_metadata."""
    project = await _make_project(db_session, name="Sensitive List")
    await _add_member(db_session, project.id, "viewer")
    _task, _job, _artifact = await _make_job(
        db_session, project, polling_metadata=_SENSITIVE_POLLING_METADATA
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    job_data = resp.json()["compute_jobs"][0]
    metadata = job_data["polling_metadata"]
    assert "env" not in metadata
    assert "command" not in metadata
    assert "cwd" not in metadata
    # Non-sensitive fields are preserved
    assert metadata.get("status_message") == "running"
    assert metadata.get("result_metrics") == {"accuracy": 0.95}


@pytest.mark.asyncio
async def test_kill_compute_job_strips_sensitive_polling_metadata(
    client: AsyncClient, db_session
):
    """Kill endpoint must not expose env, command, or cwd in polling_metadata."""
    project = await _make_project(db_session, name="Sensitive Kill")
    await _add_member(db_session, project.id, "member")
    _task, job, _artifact = await _make_job(
        db_session, project, polling_metadata=_SENSITIVE_POLLING_METADATA
    )

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 200
    metadata = resp.json()["compute_job"]["polling_metadata"]
    assert "env" not in metadata
    assert "command" not in metadata
    assert "cwd" not in metadata
    # Non-sensitive fields survive (result_metrics is not mutated by the kill path)
    assert metadata.get("result_metrics") == {"accuracy": 0.95}


# ---------------------------------------------------------------------------
# Success criterion / verdict serialization tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_compute_jobs_includes_success_criterion_and_verdict(
    client: AsyncClient, db_session
):
    """The list endpoint augments each job with the declared success criterion
    from its task payload and the evaluated verdict from plan_output."""
    project = await _make_project(db_session, name="Criterion Project")
    await _add_member(db_session, project.id, "viewer")

    criterion = {"metric": "accuracy", "operator": ">=", "threshold": 0.9}
    verdict = {
        "metric": "accuracy",
        "operator": ">=",
        "threshold": 0.9,
        "value": 0.95,
        "met": True,
        "measurable": True,
    }
    task = Task(
        project_id=project.id,
        title="criterion task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "success_criterion": criterion}],
        plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": verdict}},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        accrued_cost_usd=1.0,
        completed_at=datetime.utcnow(),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1

    assert jobs[0]["success_criterion"] == criterion
    assert jobs[0]["criterion_verdict"] == verdict
    assert jobs[0]["criterion_verdict"]["met"] is True
    assert jobs[0]["criterion_verdict"]["value"] == 0.95


@pytest.mark.asyncio
async def test_list_compute_jobs_no_criterion_returns_null(
    client: AsyncClient, db_session
):
    """When the task has no declared success_criterion, both fields are null."""
    project = await _make_project(db_session, name="No Criterion Project")
    await _add_member(db_session, project.id, "viewer")
    _task, _job, _artifact = await _make_job(db_session, project)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["success_criterion"] is None
    assert jobs[0]["criterion_verdict"] is None


@pytest.mark.asyncio
async def test_list_compute_jobs_criterion_verdict_recovered_from_job_row(
    client: AsyncClient, db_session
):
    """Terminal job with result_metrics but task not yet finalized (no plan_output
    compute_result.criterion) — verdict is computed on-the-fly from the job row."""
    project = await _make_project(db_session, name="Unfinalized Criterion")
    await _add_member(db_session, project.id, "viewer")

    criterion = {"metric": "accuracy", "operator": ">=", "threshold": 0.9}
    task = Task(
        project_id=project.id,
        title="unfinalized task",
        status=TaskStatus.IN_PROGRESS,  # task not yet finalized
        payloads=[{"type": "compute_experiment", "success_criterion": criterion}],
        plan_output={},  # no compute_result.criterion yet
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        accrued_cost_usd=1.0,
        final_cost_usd=1.0,
        completed_at=datetime.utcnow(),
        polling_metadata={"result_metrics": {"accuracy": 0.95}},
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1

    assert jobs[0]["success_criterion"] == criterion
    verdict = jobs[0]["criterion_verdict"]
    assert verdict is not None, "criterion_verdict must be computed from the job row"
    assert verdict["metric"] == "accuracy"
    assert verdict["met"] is True
    assert verdict["value"] == pytest.approx(0.95)
    assert verdict["measurable"] is True


@pytest.mark.asyncio
async def test_list_compute_jobs_criterion_verdict_recovered_from_stdout(
    client: AsyncClient, db_session
):
    """Terminal job with metrics only in command_log stdout (no result_metrics),
    task not yet finalized — verdict recovered via the stdout-JSON fallback."""
    project = await _make_project(db_session, name="Stdout Criterion")
    await _add_member(db_session, project.id, "viewer")

    criterion = {"metric": "f1", "operator": ">=", "threshold": 0.8}
    task = Task(
        project_id=project.id,
        title="stdout fallback task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "success_criterion": criterion}],
        plan_output={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        accrued_cost_usd=0.5,
        final_cost_usd=0.5,
        completed_at=datetime.utcnow(),
        polling_metadata={
            "command_log": {"text": 'run complete\n{"f1": 0.88}\n', "truncated": False}
        },
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1

    assert jobs[0]["success_criterion"] == criterion
    verdict = jobs[0]["criterion_verdict"]
    assert verdict is not None, "criterion_verdict must be recovered from stdout JSON"
    assert verdict["metric"] == "f1"
    assert verdict["met"] is True
    assert verdict["value"] == pytest.approx(0.88)
    assert verdict["measurable"] is True


@pytest.mark.asyncio
async def test_list_compute_jobs_all_terminal_returns_old_jobs(
    client: AsyncClient, db_session
):
    """Terminal jobs older than the default 7-day window must be excluded by
    default but returned when ?all_terminal=true is passed.

    Creates a job with completed_at 30 days ago (well outside the 7-day cap),
    confirms the default endpoint excludes it, then confirms ?all_terminal=true
    surfaces it alongside any active jobs.
    """
    project = await _make_project(db_session, name="Old Jobs Project")
    await _add_member(db_session, project.id, "viewer")

    # Seed a terminal job completed 30 days ago — beyond any allowed window.
    _task, old_job, _artifact = await _make_job(
        db_session, project, status=ComputeJobStatus.SUCCEEDED
    )
    old_job.completed_at = datetime.now(tz=timezone.utc) - timedelta(days=30)
    db_session.add(old_job)
    await db_session.commit()

    # Default (time-windowed) request must NOT include the old job.
    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    ids = [j["id"] for j in resp.json()["compute_jobs"]]
    assert str(old_job.id) not in ids, "old terminal job must be excluded by default"

    # ?all_terminal=true must include the old job.
    resp = await client.get(
        f"/api/admin/projects/{project.id}/compute-jobs?all_terminal=true"
    )
    assert resp.status_code == 200
    ids = [j["id"] for j in resp.json()["compute_jobs"]]
    assert str(old_job.id) in ids, "old terminal job must appear with all_terminal=true"


# ---------------------------------------------------------------------------
# Analyze endpoint tests
# ---------------------------------------------------------------------------


async def _make_terminal_job_with_task(
    session,
    project,
    *,
    status=ComputeJobStatus.SUCCEEDED,
    plan_output=None,
    result_reference="gs://bucket/jobs/job-1/result.json",
):
    """Seed a terminal compute job with an optional plan_output on its task."""
    task = Task(
        project_id=project.id,
        title="compute experiment task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "experiment": "exp_v1"}],
        plan_output=plan_output or {},
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
        spend_cap_usd=5.0,
        accrued_cost_usd=1.5,
        result_reference=result_reference,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return task, job


async def _seed_knowledge_doc(session, project, task):
    """Seed a deterministic compute-experiment KnowledgeDocument for task."""
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
        title="Compute experiment: exp_v1 — SUCCESS",
        summary="Outcome: SUCCESS.",
        body="# Compute experiment verdict\n- **Outcome:** SUCCESS",
        topic="exp_v1",
        tags=["compute_experiment", "SUCCESS"],
        source_citations=[],
        forward_quality_verdict={
            "strategy": "metric",
            "outcome": "SUCCESS",
            "analysis_mode": "deterministic",
        },
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


@pytest.mark.asyncio
async def test_analyze_compute_job_missing_404(client: AsyncClient, db_session):
    resp = await client.post(f"/api/admin/compute-jobs/{uuid.uuid4()}/analyze")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_analyze_compute_job_viewer_forbidden(client: AsyncClient, db_session):
    project = await _make_project(db_session, name="Analyze Viewer")
    await _add_member(db_session, project.id, "viewer")
    _task, job = await _make_terminal_job_with_task(db_session, project)

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/analyze")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_analyze_compute_job_nonterminal_returns_409(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session, name="Analyze Running")
    await _add_member(db_session, project.id, "member")
    _task, job, _artifact = await _make_job(
        db_session, project, status=ComputeJobStatus.RUNNING
    )

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/analyze")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_analyze_compute_job_member_ok(client: AsyncClient, db_session):
    """A terminal job returns 200 with status=upgraded when analysis succeeds (mocked)."""
    project = await _make_project(db_session, name="Analyze Member OK")
    await _add_member(db_session, project.id, "member")
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 1.5}},
    )
    doc = await _seed_knowledge_doc(db_session, project, task)

    canned_envelope = {
        "status": "upgraded",
        "reason": None,
        "analysis_mode": "llm",
        "knowledge_document_id": str(doc.id),
    }
    with patch(
        "app.knowledge_completion.analyze_compute_experiment_knowledge",
        new=AsyncMock(return_value=canned_envelope),
    ):
        resp = await client.post(f"/api/admin/compute-jobs/{job.id}/analyze")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "upgraded"
    assert body["analysis_mode"] == "llm"


@pytest.mark.asyncio
async def test_list_compute_jobs_includes_summary(client: AsyncClient, db_session):
    """The list response includes a summary block aggregating job_count,
    total_spend_usd, criterion_met_count, measured_count, and outcome_breakdown
    computed over the returned page without altering existing per-job fields."""
    project = await _make_project(db_session, name="Summary Project")
    await _add_member(db_session, project.id, "viewer")

    # Job 1: terminal (succeeded), criterion met, measurable, final_cost_usd set.
    criterion_met = {
        "metric": "accuracy",
        "operator": ">=",
        "threshold": 0.9,
        "value": 0.95,
        "met": True,
        "measurable": True,
    }
    task1 = Task(
        project_id=project.id,
        title="met task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9}}],
        plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": criterion_met}},
    )
    db_session.add(task1)
    await db_session.commit()
    await db_session.refresh(task1)
    job1 = ComputeJob(
        task_id=task1.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        accrued_cost_usd=1.0,
        final_cost_usd=1.5,
        completed_at=datetime.utcnow(),
    )
    db_session.add(job1)
    await db_session.commit()

    # Job 2: terminal (failed), criterion unmet (measurable but not met).
    criterion_unmet = {
        "metric": "accuracy",
        "operator": ">=",
        "threshold": 0.9,
        "value": 0.75,
        "met": False,
        "measurable": True,
    }
    task2 = Task(
        project_id=project.id,
        title="unmet task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9}}],
        plan_output={"compute_result": {"outcome": "FAILURE", "criterion": criterion_unmet}},
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)
    job2 = ComputeJob(
        task_id=task2.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.FAILED,
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        accrued_cost_usd=0.8,
        final_cost_usd=0.8,
        completed_at=datetime.utcnow(),
    )
    db_session.add(job2)
    await db_session.commit()

    # Job 3: running (no criterion, only accrued cost).
    task3 = Task(
        project_id=project.id,
        title="running task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task3)
    await db_session.commit()
    await db_session.refresh(task3)
    job3 = ComputeJob(
        task_id=task3.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        accrued_cost_usd=0.3,
    )
    db_session.add(job3)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    body = resp.json()

    # Existing keys must be preserved.
    assert "compute_jobs" in body
    assert "has_more" in body
    assert "limit" in body
    assert "offset" in body
    assert len(body["compute_jobs"]) == 3

    summary = body["summary"]
    assert summary["total_jobs"] == 3
    # total_spend: job1 final=1.5, job2 final=0.8, job3 accrued=0.3 (no final)
    assert summary["total_spend_usd"] == pytest.approx(2.6)
    # criterion_met: only job1 (plan_output.compute_result.criterion.met = true)
    assert summary["criterion_met"] == 1
    # measured: job1 + job2 (both have plan_output.compute_result.criterion set)
    assert summary["criterion_measured"] == 2
    breakdown = summary["outcome_breakdown"]
    assert breakdown[ComputeJobStatus.SUCCEEDED.value] == 1
    assert breakdown[ComputeJobStatus.FAILED.value] == 1
    assert breakdown[ComputeJobStatus.RUNNING.value] == 1


@pytest.mark.asyncio
async def test_analyze_endpoint_persists_unavailable_cause(
    client: AsyncClient, db_session
):
    """When analysis ends unavailable, the cause is stamped on the stored doc's
    forward_quality_verdict without changing analysis_mode, and the endpoint
    returns status=analysis_unavailable with the doc id."""
    project = await _make_project(db_session, name="Analyze Unavailable")
    await _add_member(db_session, project.id, "member")
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 1.5}},
    )
    doc = await _seed_knowledge_doc(db_session, project, task)
    original_mode = doc.forward_quality_verdict.get("analysis_mode")

    # Force the analysis-disabled path for deterministic behavior.
    with patch.dict("os.environ", {"DISABLE_LLM_COMPUTE_ANALYSIS": "1"}):
        resp = await client.post(f"/api/admin/compute-jobs/{job.id}/analyze")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "analysis_unavailable"
    assert body["reason"] == "analysis-disabled"
    assert body.get("knowledge_document_id") == str(doc.id)

    # The stored doc must carry the unavailable_cause stamp.
    await db_session.refresh(doc)
    verdict = doc.forward_quality_verdict
    assert verdict.get("unavailable_cause") == "analysis-disabled"
    assert verdict.get("analysis_attempted_at") is not None
    # analysis_mode must not have changed.
    assert verdict.get("analysis_mode") == original_mode
