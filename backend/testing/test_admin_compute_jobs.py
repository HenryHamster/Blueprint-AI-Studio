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
    LocalRunner,
    Project,
    ProjectMember,
    SourceKind,
    Task,
    TaskStatus,
    _utcnow,
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
    task, job, artifact = await _make_job(
        db_session,
        project,
        title="Killable compute task",
        with_artifact=True,
    )
    assert artifact is not None

    resp = await client.post(f"/api/admin/compute-jobs/{job.id}/kill")
    assert resp.status_code == 200
    body = resp.json()["compute_job"]
    assert body["status"] == ComputeJobStatus.CANCELLED.value
    assert body["outcome"] == ComputeJobOutcome.CANCELLED.value
    assert "operator" in (body["terminal_reason"] or "")
    assert body["task_title"] == task.title
    assert body["task_type"] == "compute_experiment"
    assert body["artifact"] == {
        "id": str(artifact.id),
        "artifact_type": ArtifactType.COMPUTE_RESULT.value,
        "storage_uri": "gs://bucket/jobs/job-1/result.parquet",
        "logical_key": "compute-result-job-1",
        "created_at": artifact.created_at.isoformat(),
    }


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
async def test_list_compute_jobs_redacts_credentials_in_per_arm_text_fields(
    client: AsyncClient, db_session
):
    """serialize_compute_job_response_safe must redact credential-bearing patterns
    from per-arm log/note/label text inside result_metrics.arms so VM stdout that
    echoed a secret does not reach the operator-facing API."""
    project = await _make_project(db_session, name="Arm Redact")
    await _add_member(db_session, project.id, "viewer")
    _task, _job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "result_metrics": {
                "accuracy": 0.9,
                "arms": {
                    "control": {
                        "accuracy": 0.85,
                        "log": "run ok\nexport ANTHROPIC_API_KEY=sk-ant-api03-REDACTME\ndone",
                        "note": "baseline run",
                        "label": "control-arm",
                    },
                    "treatment": {
                        "accuracy": 0.92,
                        "log": "completed with ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH token",
                        "note": "treatment note",
                        "label": "treatment-arm",
                    },
                },
            }
        },
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    pm = resp.json()["compute_jobs"][0]["polling_metadata"]
    arms = pm["result_metrics"]["arms"]

    # Credential-bearing patterns must be scrubbed from log text.
    assert "sk-ant-api03-REDACTME" not in arms["control"]["log"]
    assert "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH" not in arms["treatment"]["log"]

    # Non-sensitive fields survive intact.
    assert arms["control"]["note"] == "baseline run"
    assert arms["control"]["label"] == "control-arm"
    assert arms["treatment"]["note"] == "treatment note"
    assert arms["treatment"]["label"] == "treatment-arm"
    assert pm["result_metrics"]["accuracy"] == 0.9


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
async def test_compute_job_arm_log_credentials_redacted(
    client: AsyncClient, db_session
):
    """Credentials in per-arm log/note/label must be redacted; numeric fields unchanged."""
    project = await _make_project(db_session, name="Arm Redact")
    await _add_member(db_session, project.id, "viewer")
    _task, _job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "result_metrics": {
                "arms": {
                    "arm_a": {
                        "log": "run complete TOKEN=secret-token-abc123 done",
                        "note": "export API_KEY=my-secret-key",
                        "label": "clean label",
                        "score": 0.87,
                    },
                    "arm_b": {
                        "log": "normal log",
                        "score": 0.91,
                    },
                },
                "best_arm": "arm_b",
            }
        },
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    pm = resp.json()["compute_jobs"][0]["polling_metadata"]
    arms = pm["result_metrics"]["arms"]

    # Credentials must be redacted.
    assert "secret-token-abc123" not in arms["arm_a"]["log"]
    assert "my-secret-key" not in arms["arm_a"]["note"]
    # Clean text and numeric values must be preserved.
    assert arms["arm_a"]["label"] == "clean label"
    assert arms["arm_a"]["score"] == 0.87
    assert arms["arm_b"]["log"] == "normal log"
    assert arms["arm_b"]["score"] == 0.91
    assert pm["result_metrics"]["best_arm"] == "arm_b"


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
        completed_at=_utcnow(),
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
async def test_list_compute_jobs_per_arm_verdicts_exposed(
    client: AsyncClient, db_session
):
    """The list endpoint exposes per_arm_verdicts from plan_output.knowledge_document."""
    project = await _make_project(db_session, name="Per-Arm Verdicts Project")
    await _add_member(db_session, project.id, "viewer")

    per_arm = {
        "arm_a": {"value": 0.92, "met": True, "cost_usd": 0.30},
        "arm_b": {"value": 0.88, "met": False, "cost_usd": 0.90},
    }
    task = Task(
        project_id=project.id,
        title="per-arm task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment"}],
        plan_output={
            "compute_result": {"outcome": "SUCCESS", "final_cost_usd": 1.20},
            "knowledge_document": {"per_arm_verdicts": per_arm},
        },
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
        accrued_cost_usd=1.20,
        completed_at=_utcnow(),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs = resp.json()["compute_jobs"]
    assert len(jobs) == 1

    assert jobs[0]["per_arm_verdicts"] == per_arm
    assert jobs[0]["per_arm_verdicts"]["arm_a"]["cost_usd"] == pytest.approx(0.30)
    assert jobs[0]["per_arm_verdicts"]["arm_b"]["met"] is False


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
        completed_at=_utcnow(),
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
        completed_at=_utcnow(),
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


@pytest.mark.asyncio
async def test_list_compute_jobs_all_terminal_summary_matches_list(
    client: AsyncClient, db_session
):
    """When ?all_terminal=true, the summary totals must match the returned job list.

    Seeds one old terminal job (30 days ago, outside any default window) plus one
    active job, then asserts:
    - Default request: old terminal job excluded from both list AND summary.
    - ?all_terminal=true: old terminal job counted in summary terminal_jobs/total_jobs
      and total_spend_usd matches the combined spend of the full returned list.
    """
    project = await _make_project(db_session, name="Summary Alignment Project")
    await _add_member(db_session, project.id, "viewer")

    # Old terminal job completed 30 days ago with a known cost.
    _task_old, old_job, _art = await _make_job(
        db_session, project, status=ComputeJobStatus.SUCCEEDED
    )
    old_job.completed_at = datetime.now(tz=timezone.utc) - timedelta(days=30)
    old_job.final_cost_usd = 3.0
    db_session.add(old_job)
    await db_session.commit()

    # Active job (still running, no final_cost).
    _task_run, running_job, _art2 = await _make_job(
        db_session, project, status=ComputeJobStatus.RUNNING
    )
    running_job.accrued_cost_usd = 1.0
    db_session.add(running_job)
    await db_session.commit()

    # --- Default request: old terminal job must NOT appear in list or summary ---
    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    body = resp.json()
    default_ids = {j["id"] for j in body["compute_jobs"]}
    assert str(old_job.id) not in default_ids, "old job excluded from default list"
    default_summary = body["summary"]
    # Only the running job is in scope; terminal_jobs count must not include the old one.
    assert default_summary["terminal_jobs"] == 0, (
        f"default summary must exclude old terminal job; got {default_summary}"
    )

    # --- ?all_terminal=true: summary totals must align with the returned list ---
    resp = await client.get(
        f"/api/admin/projects/{project.id}/compute-jobs?all_terminal=true"
    )
    assert resp.status_code == 200
    body = resp.json()
    all_ids = {j["id"] for j in body["compute_jobs"]}
    assert str(old_job.id) in all_ids, "old job must appear with all_terminal=true"

    summary = body["summary"]
    returned_jobs = body["compute_jobs"]
    # Summary total_jobs must equal the number of jobs in the list.
    assert summary["total_jobs"] == len(returned_jobs), (
        f"summary total_jobs={summary['total_jobs']} != list len={len(returned_jobs)}"
    )
    # Old terminal job must be counted in terminal_jobs.
    assert summary["terminal_jobs"] >= 1, (
        f"terminal_jobs must include old job; got {summary['terminal_jobs']}"
    )
    # total_spend_usd must include the old job's cost (3.0) plus running accrued (1.0).
    assert summary["total_spend_usd"] == pytest.approx(4.0), (
        f"expected spend=4.0 (old 3.0 + running 1.0), got {summary['total_spend_usd']}"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/compute-jobs/{job_id} — single-job detail endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_compute_job_detail_returns_arms(client: AsyncClient, db_session):
    """Fetching one job by id returns the full detail including per-arm result block."""
    project = await _make_project(db_session, name="Detail Arms Project")
    await _add_member(db_session, project.id, "viewer")
    _task, job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "result_metrics": {
                "arms": {
                    "arm_a": {"log": "arm_a complete", "score": 0.87},
                    "arm_b": {"log": "arm_b complete", "score": 0.91},
                },
                "best_arm": "arm_b",
            }
        },
    )

    resp = await client.get(f"/api/admin/compute-jobs/{job.id}")
    assert resp.status_code == 200
    body = resp.json()["compute_job"]
    assert body["id"] == str(job.id)
    arms = body["polling_metadata"]["result_metrics"]["arms"]
    assert "arm_a" in arms
    assert "arm_b" in arms
    assert arms["arm_a"]["score"] == pytest.approx(0.87)
    assert arms["arm_b"]["score"] == pytest.approx(0.91)
    assert body["polling_metadata"]["result_metrics"]["best_arm"] == "arm_b"


@pytest.mark.asyncio
async def test_get_compute_job_detail_viewer_ok(client: AsyncClient, db_session):
    """Viewer-role user can fetch job detail including task_title and task_type."""
    project = await _make_project(db_session, name="Detail Viewer OK")
    await _add_member(db_session, project.id, "viewer")
    _task, job, _artifact = await _make_job(db_session, project, with_artifact=True)

    resp = await client.get(f"/api/admin/compute-jobs/{job.id}")
    assert resp.status_code == 200
    body = resp.json()["compute_job"]
    assert body["id"] == str(job.id)
    assert body["task_title"] == "compute_experiment task"
    assert body["task_type"] == "compute_experiment"
    assert body["artifact"] is not None
    assert body["artifact"]["storage_uri"] == "gs://bucket/jobs/job-1/result.parquet"


@pytest.mark.asyncio
async def test_get_compute_job_detail_missing_404(client: AsyncClient, db_session):
    """Unknown job id returns 404."""
    resp = await client.get(f"/api/admin/compute-jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_compute_job_detail_non_member_404(client: AsyncClient, db_session):
    """User with no project membership gets 404 (IDOR protection)."""
    project = await _make_project(db_session, name="Detail Non Member")
    # Deliberately no membership for TEST_USER_ID.
    _task, job, _artifact = await _make_job(db_session, project)

    resp = await client.get(f"/api/admin/compute-jobs/{job.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_compute_job_detail_cross_project_404(client: AsyncClient, db_session):
    """Member of project A fetching project B's job gets 404 (IDOR protection)."""
    project_a = await _make_project(db_session, name="Detail Project A")
    await _add_member(db_session, project_a.id, "viewer")

    project_b = await _make_project(db_session, name="Detail Project B")
    _task_b, job_b, _artifact_b = await _make_job(db_session, project_b)

    resp = await client.get(f"/api/admin/compute-jobs/{job_b.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_compute_job_detail_redacts_polling_metadata(
    client: AsyncClient, db_session
):
    """env/command/cwd are stripped from the single-job detail response."""
    project = await _make_project(db_session, name="Detail Redact")
    await _add_member(db_session, project.id, "viewer")
    _task, job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "command": ["python3", "run.py"],
            "cwd": "/workspace",
            "env": {"SECRET": "top-secret"},
            "status_message": "running",
        },
    )

    resp = await client.get(f"/api/admin/compute-jobs/{job.id}")
    assert resp.status_code == 200
    pm = resp.json()["compute_job"]["polling_metadata"]
    assert "command" not in pm
    assert "cwd" not in pm
    assert "env" not in pm
    assert pm.get("status_message") == "running"


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
    polling_metadata=None,
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
        polling_metadata=polling_metadata,
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
        completed_at=_utcnow(),
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
        completed_at=_utcnow(),
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


@pytest.mark.asyncio
async def test_list_compute_jobs_redacts_credentials_in_result_metrics(
    client: AsyncClient, db_session
):
    """result_metrics string values must have credentials redacted recursively."""
    project = await _make_project(db_session, name="Redact Result Metrics")
    await _add_member(db_session, project.id, "viewer")
    # Seed a job whose result_metrics contains credential-bearing strings at
    # various nesting depths, plus a safe numeric value that must survive.
    await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "result_metrics": {
                "accuracy": 0.95,
                "log": "export ANTHROPIC_API_KEY=sk-ant-abc123 run complete",
                "arms": {
                    "arm_a": {
                        "note": "Authorization: Bearer ghp_token123 step done",
                        "score": 0.88,
                    }
                },
            }
        },
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    rm = resp.json()["compute_jobs"][0]["polling_metadata"]["result_metrics"]

    # Numeric values survive unchanged.
    assert rm["accuracy"] == 0.95
    # Top-level credential string must be redacted.
    assert "sk-ant-abc123" not in rm["log"]
    assert "ANTHROPIC_API_KEY" not in rm["log"] or "***" in rm["log"]
    # Nested string inside arms must also be redacted.
    assert "ghp_token123" not in rm["arms"]["arm_a"]["note"]
    # Numeric field inside arms survives.
    assert rm["arms"]["arm_a"]["score"] == 0.88


@pytest.mark.asyncio
async def test_aggregate_summary_endpoint_returns_all_jobs(
    client: AsyncClient, db_session
):
    """The /compute-jobs/summary endpoint returns totals over ALL jobs regardless of age."""
    project = await _make_project(db_session, name="Agg Summary Project")
    await _add_member(db_session, project.id, "viewer")

    # Old terminal job (would be outside any include_finished_minutes window).
    old_task = Task(
        project_id=project.id,
        title="old task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "success_criterion": {"metric": "acc", "operator": ">=", "threshold": 0.9}}],
        plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": {"met": True, "measurable": True}}},
    )
    db_session.add(old_task)
    await db_session.commit()
    await db_session.refresh(old_task)
    old_job = ComputeJob(
        task_id=old_task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        final_cost_usd=2.0,
        completed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(old_job)
    await db_session.commit()

    # Recent running job.
    new_task = Task(
        project_id=project.id,
        title="new task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(new_task)
    await db_session.commit()
    await db_session.refresh(new_task)
    new_job = ComputeJob(
        task_id=new_task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        accrued_cost_usd=0.5,
    )
    db_session.add(new_job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs/summary")
    assert resp.status_code == 200
    body = resp.json()

    # Both jobs are included (old terminal + new running).
    assert body["total_jobs"] == 2
    assert body["active_jobs"] == 1
    assert body["terminal_jobs"] == 1
    assert body["total_spend_usd"] == pytest.approx(2.5)  # 2.0 final + 0.5 accrued
    assert body["outcome_breakdown"][ComputeJobStatus.SUCCEEDED.value] == 1
    assert body["outcome_breakdown"][ComputeJobStatus.RUNNING.value] == 1
    # old_task has criterion data.
    assert body["criterion_measured"] == 1
    assert body["criterion_met"] == 1


@pytest.mark.asyncio
async def test_aggregate_summary_endpoint_non_member_returns_404(
    client: AsyncClient, db_session
):
    """The summary endpoint returns 404 for users without project access."""
    project = await _make_project(db_session, name="Agg Summary No-Access")
    # No member row — request must be rejected.
    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs/summary")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_aggregate_summary_endpoint_empty_project(
    client: AsyncClient, db_session
):
    """The summary endpoint returns zero-value fields when no jobs exist."""
    project = await _make_project(db_session, name="Agg Summary Empty")
    await _add_member(db_session, project.id, "viewer")

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_jobs"] == 0
    assert body["active_jobs"] == 0
    assert body["terminal_jobs"] == 0
    assert body["total_spend_usd"] == pytest.approx(0.0)
    assert body["outcome_breakdown"] == {}
    assert body["criterion_measured"] == 0
    assert body["criterion_met"] == 0


# ---------------------------------------------------------------------------
# sweep-groups endpoint tests
# ---------------------------------------------------------------------------


async def _seed_sweep_doc(session, project, *, sweep_id, arm_label, criterion_met=True):
    """Seed one planned compute arm and its matching KnowledgeDocument."""
    task = Task(
        project_id=project.id,
        title=f"sweep task {arm_label}",
        status=TaskStatus.COMPLETED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "sweep_id": sweep_id,
                "experiment": arm_label,
            }
        ],
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=task.id,
        source_kind=SourceKind.COMPUTE_EXPERIMENT.value,
        title=f"arm {arm_label}",
        summary="Summary.",
        body="# Body",
        topic="exp",
        sweep_id=sweep_id,
        arm_label=arm_label,
        forward_quality_verdict={
            "analysis_mode": "deterministic",
            "criterion_met": criterion_met,
        },
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


@pytest.mark.asyncio
async def test_sweep_groups_endpoint_viewer_ok(client: AsyncClient, db_session):
    """A viewer can retrieve sweep groups; response shape includes sweep_id key."""
    project = await _make_project(db_session, name="Sweep Groups Viewer")
    await _add_member(db_session, project.id, "viewer")
    # Two arms in one sweep — minimum for build_compute_sweep_groups to include it.
    await _seed_sweep_doc(db_session, project, sweep_id="sw-abc", arm_label="baseline")
    await _seed_sweep_doc(
        db_session, project, sweep_id="sw-abc", arm_label="treatment"
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs/sweep-groups")
    assert resp.status_code == 200
    body = resp.json()
    assert "sweep_groups" in body
    groups = body["sweep_groups"]
    assert "sw-abc" in groups
    assert len(groups["sw-abc"]) == 2
    arm_labels = {e["arm_label"] for e in groups["sw-abc"]}
    assert arm_labels == {"baseline", "treatment"}


@pytest.mark.asyncio
async def test_sweep_groups_endpoint_omits_incomplete_planned_sweep(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session, name="Sweep Groups Incomplete")
    await _add_member(db_session, project.id, "viewer")
    await _seed_sweep_doc(db_session, project, sweep_id="sw-missing", arm_label="baseline")
    missing_task = Task(
        project_id=project.id,
        title="missing sweep task",
        status=TaskStatus.FAILED,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "sweep_id": "sw-missing",
                "experiment": "treatment",
            }
        ],
    )
    db_session.add(missing_task)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs/sweep-groups")
    assert resp.status_code == 200
    assert resp.json()["sweep_groups"] == {}


@pytest.mark.asyncio
async def test_sweep_groups_endpoint_non_member_404(client: AsyncClient, db_session):
    """A user with no project membership receives 404 from the sweep-groups endpoint."""
    project = await _make_project(db_session, name="Sweep Groups No Access")
    # No ProjectMember row — request must be rejected.
    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs/sweep-groups")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/admin/compute-jobs/{job_id}/ask — grounded Q&A endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_compute_job_grounded_answer(client: AsyncClient, db_session):
    """A member can ask a free-form question and receive a grounded answer with citations."""
    project = await _make_project(db_session, name="Ask Grounded Project")
    await _add_member(db_session, project.id, "member")
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 1.5}},
    )

    canned = {
        "answer": "The accuracy metric was 0.95, meeting the declared criterion.",
        "citations": ["gs://bucket/results/exp.json#metric:accuracy=0.95"],
        "grounding": {"checked": True, "file_citations": 1, "grounded": 1, "unresolved": []},
        "grounding_miss": False,
        "fallback_reason": None,
    }
    with patch(
        "app.knowledge_completion.answer_compute_experiment_question",
        new=AsyncMock(return_value=canned),
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": "What accuracy did the experiment achieve?"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "The accuracy metric was 0.95, meeting the declared criterion."
    assert body["citations"] == ["gs://bucket/results/exp.json#metric:accuracy=0.95"]
    assert body["grounding_miss"] is False
    assert body["grounding"]["grounded"] == 1


@pytest.mark.asyncio
async def test_ask_compute_job_viewer_forbidden(client: AsyncClient, db_session):
    """Viewers must not be able to use the ask endpoint (member+ only)."""
    project = await _make_project(db_session, name="Ask Viewer Forbidden")
    await _add_member(db_session, project.id, "viewer")
    _task, job = await _make_terminal_job_with_task(db_session, project)

    resp = await client.post(
        f"/api/admin/compute-jobs/{job.id}/ask",
        json={"question": "What happened?"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_ask_compute_job_nonterminal_returns_409(client: AsyncClient, db_session):
    """Asking about a running job returns 409 (Q&A requires terminal job)."""
    project = await _make_project(db_session, name="Ask Running Job")
    await _add_member(db_session, project.id, "member")
    _task, job, _artifact = await _make_job(
        db_session, project, status=ComputeJobStatus.RUNNING
    )

    resp = await client.post(
        f"/api/admin/compute-jobs/{job.id}/ask",
        json={"question": "Is it done?"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_ask_compute_job_no_evidence_declines_honestly(
    client: AsyncClient, db_session
):
    """When the run has no durable evidence, the endpoint returns a polite decline."""
    project = await _make_project(db_session, name="Ask No Evidence")
    await _add_member(db_session, project.id, "member")
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS"}},
        result_reference=None,
    )

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test", "DISABLE_LLM_COMPUTE_ANALYSIS": "0"}):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": "What metrics were reported?"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "answer" in body
    assert "citations" in body
    assert body["grounding_miss"] is True
    assert isinstance(body["citations"], list)


@pytest.mark.asyncio
async def test_ask_compute_job_metric_only_job_answers_grounded(
    client: AsyncClient, db_session
):
    """Metric-only terminal jobs without artifact_id still support grounded Q&A."""
    project = await _make_project(db_session, name="Ask Metric Only")
    await _add_member(db_session, project.id, "member")
    _task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS"}},
        result_reference=None,
        polling_metadata={"result_metrics": {"accuracy": 0.95}},
    )

    metric_citation = f"compute-job:{job.id}#metric:accuracy=0.95"

    class _FakeContent:
        text = (
            f'{{"answer": "The run achieved 0.95 accuracy.", '
            f'"citations": ["{metric_citation}"]}}'
        )

    class _FakeResponse:
        content = [_FakeContent()]

    class _FakeMessages:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    with (
        patch("anthropic.AsyncAnthropic", return_value=_FakeClient()),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": "What accuracy did it reach?"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "The run achieved 0.95 accuracy."
    assert body["grounding_miss"] is False
    assert body["citations"] == [metric_citation]


@pytest.mark.asyncio
async def test_ask_endpoint_returns_grounded_answer_and_rejects_ungrounded(
    client: AsyncClient, db_session
):
    """Grounded Q&A: answer path returns grounding_miss=False; no-evidence path returns grounding_miss=True."""
    project = await _make_project(db_session, name="Ask Grounded vs Ungrounded")
    await _add_member(db_session, project.id, "member")
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 2.0}},
    )

    # --- grounded answer path ---
    grounded_response = {
        "answer": "Sharpe ratio was 1.43, exceeding the 1.2 threshold.",
        "citations": ["gs://bp-artifacts/proj/task/result/#metric:sharpe_ratio=1.43"],
        "grounding": {"checked": True, "file_citations": 1, "grounded": 1, "unresolved": []},
        "grounding_miss": False,
        "fallback_reason": None,
    }
    with patch(
        "app.knowledge_completion.answer_compute_experiment_question",
        new=AsyncMock(return_value=grounded_response),
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": "What was the Sharpe ratio?"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Sharpe ratio was 1.43, exceeding the 1.2 threshold."
    assert body["grounding_miss"] is False
    assert body["grounding"]["grounded"] == 1
    assert body["fallback_reason"] is None

    # --- ungrounded / no-evidence path ---
    ungrounded_response = {
        "answer": None,
        "citations": [],
        "grounding": {"checked": True, "file_citations": 0, "grounded": 0, "unresolved": []},
        "grounding_miss": True,
        "fallback_reason": "no-evidence",
    }
    with patch(
        "app.knowledge_completion.answer_compute_experiment_question",
        new=AsyncMock(return_value=ungrounded_response),
    ):
        resp2 = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": "What was the p-value?"},
        )

    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["answer"] is None
    assert body2["grounding_miss"] is True
    assert body2["fallback_reason"] == "no-evidence"
    assert body2["citations"] == []


@pytest.mark.asyncio
async def test_ask_compute_job_includes_per_arm_evidence(
    client: AsyncClient, db_session
):
    """_gather_compute_evidence exposes per-arm sweep results so operators can ask
    'which arm won?' and receive a grounded, arm-citing answer."""
    project = await _make_project(db_session, name="Ask Arm Evidence Project")
    await _add_member(db_session, project.id, "member")

    task = Task(
        project_id=project.id,
        title="sweep experiment",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment", "experiment": "arm_sweep_v1"}],
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 2.0}},
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
        spend_cap_usd=5.0,
        accrued_cost_usd=2.0,
        result_reference="gs://bucket/sweeps/sweep-1/result.json",
        polling_metadata={
            "result_metrics": {
                "accuracy": 0.91,
                "arms": {
                    "control": {"accuracy": 0.85, "score": 0.80},
                    "treatment": {"accuracy": 0.95, "score": 0.92},
                },
            }
        },
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    captured_prompts: list[str] = []

    class _FakeContent:
        text = '{"answer": "treatment arm won with 0.95 accuracy", "citations": ["gs://bucket/sweeps/sweep-1/result.json#arms:treatment"]}'

    class _FakeResponse:
        content = [_FakeContent()]

    class _FakeMessages:
        async def create(self, **kwargs):
            captured_prompts.append(
                (kwargs.get("messages") or [{}])[0].get("content", "")
            )
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    with (
        patch("anthropic.AsyncAnthropic", return_value=_FakeClient()),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": "Which arm performed better?"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # New Q&A response shape: a successful grounded answer carries grounding_miss=False
    # (this PR replaced the legacy top-level ``grounded`` boolean).
    assert body["grounding_miss"] is False
    # The arm citation must pass the allowed-set gate (arm refs added to evidence)
    assert any("arms:treatment" in c for c in body["citations"])
    # The prompt must have included the per-arm section so the LLM saw arm data
    assert len(captured_prompts) >= 1
    prompt_text = captured_prompts[0]
    assert "Per-arm results" in prompt_text
    assert "control" in prompt_text
    assert "treatment" in prompt_text


@pytest.mark.asyncio
async def test_ask_compute_job_supplementary_citation_not_grounded(
    client: AsyncClient, db_session
):
    """When the LLM cites only supplementary citations (signed URLs, not durable evidence),
    the response sets grounded=False and returns an empty citations list.

    This test exercises the durable_cits gate: the job has real metrics so
    _gather_compute_evidence returns a non-None evidence bundle, the signed URL
    is injected into the supplementary set (allowed_set) via a mocked storage
    helper, and the LLM mock cites that signed URL.  The durable_cits filter
    strips it out (https:// is not durable), producing grounding_miss=True.
    """
    _SIGNED_URL = "https://storage.example.com/signed-url?token=xyz"

    project = await _make_project(db_session, name="Ask Supplementary Project")
    await _add_member(db_session, project.id, "member")
    # Supply result_metrics so _gather_compute_evidence does not short-circuit
    # at the 'file_count <= 0 and not metrics' guard before any citation work.
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 1.0}},
        polling_metadata={"result_metrics": {"p_success": 0.95}},
    )

    class _FakeContent:
        # LLM cites only the supplementary signed URL (not in real_evidence)
        text = f'{{"answer": "The result was successful.", "citations": ["{_SIGNED_URL}"]}}'

    class _FakeResponse:
        content = [_FakeContent()]

    class _FakeMessages:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    async def _fake_signed_url(uri):
        # Inject the signed URL into the supplementary set so the LLM's citation
        # lands in allowed_set but is excluded from real_evidence_set — this is
        # exactly the path the durable_cits filter must catch.
        return _SIGNED_URL

    with (
        patch("anthropic.AsyncAnthropic", return_value=_FakeClient()),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
        patch(
            "app.storage.generate_download_signed_url",
            side_effect=_fake_signed_url,
        ),
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": "Was it successful?"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # Grounding gate: supplementary citations (signed URLs) must not satisfy
    # the durable-evidence requirement even when the LLM cites them correctly.
    assert body["grounding_miss"] is True
    # No durable citations means empty citations list
    assert body["citations"] == []
    assert body["grounding"]["grounded"] == 0


@pytest.mark.asyncio
async def test_ask_compute_job_multi_turn_history_grounded(
    client: AsyncClient, db_session
):
    """Multi-turn Q&A: prior turns appear as context-only in the prompt,
    and the new answer is still independently grounded via durable citations."""
    project = await _make_project(db_session, name="Multi-Turn History Project")
    await _add_member(db_session, project.id, "member")
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 2.0}},
        polling_metadata={"result_metrics": {"accuracy": 0.93}},
    )

    captured_prompts: list[str] = []

    class _FakeContent:
        # Citation must match the durable ref format: artifact_uri#metric:name=value
        text = '{"answer": "The accuracy was 0.93.", "citations": ["gs://bucket/jobs/job-1/result.json#metric:accuracy=0.93"]}'

    class _FakeResponse:
        content = [_FakeContent()]

    class _FakeMessages:
        async def create(self, **kwargs):
            captured_prompts.append(
                (kwargs.get("messages") or [{}])[0].get("content", "")
            )
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    prior_history = [
        {"question": "Did this run succeed?", "answer": "Yes, it succeeded."},
    ]

    with (
        patch("anthropic.AsyncAnthropic", return_value=_FakeClient()),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={
                "question": "What was the accuracy?",
                "history": prior_history,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    # Answer is grounded in durable evidence (real result_reference metric citation)
    assert body["grounding_miss"] is False
    assert len(body["citations"]) >= 1

    # Prior conversation must appear in the prompt under a context-only label
    assert len(captured_prompts) >= 1
    prompt_text = captured_prompts[0]
    assert "Prior conversation" in prompt_text
    assert "Did this run succeed?" in prompt_text
    assert "Yes, it succeeded." in prompt_text
    # History label must indicate it is NOT citable evidence
    assert "NOT evidence" in prompt_text or "context only" in prompt_text.lower()

    # The allowed-citation list must NOT include any text from the prior answer
    # (history must not bleed into the allowed-citation set)
    assert "Yes, it succeeded." not in prompt_text.split("Choose citations ONLY")[1]


@pytest.mark.asyncio
async def test_ask_compute_job_history_bounded_and_malformed_ignored(
    client: AsyncClient, db_session
):
    """Oversized history is clamped at the route boundary; malformed entries are ignored."""
    project = await _make_project(db_session, name="History Bounds Project")
    await _add_member(db_session, project.id, "member")
    _task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS"}},
        polling_metadata={"result_metrics": {"score": 0.8}},
    )

    # 10 valid turns (exceeds _QA_HISTORY_MAX_TURNS=6) plus 2 malformed entries
    oversized_history = [
        {
            "question": (
                f"history-question-{i}-" + ("Q" * 1200) + "-question-overflow"
                if i == 9
                else f"history-question-{i}"
            ),
            "answer": (
                f"history-answer-{i}-" + ("A" * 1200) + "-answer-overflow"
                if i == 9
                else f"history-answer-{i}"
            ),
        }
        for i in range(10)
    ] + [
        {"bad_key": "no question or answer"},  # malformed — missing required fields
        "not a dict",  # malformed — wrong type entirely
    ]
    long_question = "What did this very detailed compute run show? " + ("Q" * 600)
    answer_mock = AsyncMock(
        return_value={
            "answer": "The score was 0.8.",
            "citations": [],
            "grounding_miss": False,
            "fallback_reason": None,
        }
    )

    with patch(
        "app.knowledge_completion.answer_compute_experiment_question",
        answer_mock,
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={"question": long_question, "history": oversized_history},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["grounding_miss"] is False

    assert answer_mock.await_count == 1
    assert answer_mock.await_args.kwargs["question"] == long_question
    forwarded_history = answer_mock.await_args.kwargs["history"]
    assert forwarded_history == [
        {"question": "history-question-4", "answer": "history-answer-4"},
        {"question": "history-question-5", "answer": "history-answer-5"},
        {"question": "history-question-6", "answer": "history-answer-6"},
        {"question": "history-question-7", "answer": "history-answer-7"},
        {"question": "history-question-8", "answer": "history-answer-8"},
        {
            "question": "history-question-9-" + ("Q" * 981),
            "answer": "history-answer-9-" + ("A" * 983),
        },
    ]


@pytest.mark.asyncio
async def test_ask_compute_job_history_not_citable(
    client: AsyncClient, db_session
):
    """History text must not satisfy the durable-citation gate even if the LLM cites it.

    A prior answer's text is injected into the prompt as context-only. If the LLM
    tries to cite a phrase from that prior answer, the durable_cits gate must still
    reject it (it is not in the real_evidence_set).
    """
    project = await _make_project(db_session, name="History Not Citable Project")
    await _add_member(db_session, project.id, "member")
    task, job = await _make_terminal_job_with_task(
        db_session,
        project,
        plan_output={"compute_result": {"outcome": "SUCCESS", "final_cost_usd": 1.0}},
        polling_metadata={"result_metrics": {"p_val": 0.01}},
    )

    PRIOR_ANSWER_TEXT = "p_val was 0.01 in the prior run"

    class _FakeContent:
        # LLM cites text from the prior answer — this must NOT pass the durable gate
        text = f'{{"answer": "Based on prior context.", "citations": ["{PRIOR_ANSWER_TEXT}"]}}'

    class _FakeResponse:
        content = [_FakeContent()]

    class _FakeMessages:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    with (
        patch("anthropic.AsyncAnthropic", return_value=_FakeClient()),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
    ):
        resp = await client.post(
            f"/api/admin/compute-jobs/{job.id}/ask",
            json={
                "question": "Confirm the p-value?",
                "history": [{"question": "What was p_val?", "answer": PRIOR_ANSWER_TEXT}],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    # Prior answer text is not durable evidence — must be rejected by the grounding gate
    assert body["grounding_miss"] is True
    assert body["citations"] == []


# ---------------------------------------------------------------------------
# Runner identity and wait-state surfacing tests
# ---------------------------------------------------------------------------


async def _make_runner(session, project, *, hostname="worker-01", status="active"):
    """Seed a LocalRunner for the given project."""
    import hashlib
    token = "test-token-" + hostname
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    runner = LocalRunner(
        project_id=project.id,
        token_hash=token_hash,
        hostname=hostname,
        status=status,
    )
    session.add(runner)
    await session.commit()
    await session.refresh(runner)
    return runner


@pytest.mark.asyncio
async def test_list_compute_jobs_surfaces_runner_claim(client: AsyncClient, db_session):
    """A job pinned to a registered runner must surface runner_target_id,
    runner_claimed_at, and a runner descriptor (id/hostname/status) in the response."""
    project = await _make_project(db_session, name="Runner Claim Project")
    await _add_member(db_session, project.id, "viewer")
    runner = await _make_runner(db_session, project, hostname="worker-01")

    claimed_at = "2026-07-04T10:00:00Z"
    _task, job, _artifact = await _make_job(
        db_session,
        project,
        polling_metadata={
            "runner_target_id": str(runner.id),
            "runner_claimed_at": claimed_at,
        },
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    j = resp.json()["compute_jobs"][0]

    assert j["runner_target_id"] == str(runner.id)
    assert j["runner_claimed_at"] == claimed_at
    assert j["runner"] is not None
    assert j["runner"]["id"] == str(runner.id)
    assert j["runner"]["hostname"] == "worker-01"
    assert j["runner"]["status"] == "active"
    # token_hash must NOT be present in the descriptor.
    assert "token_hash" not in j["runner"]
    assert "token" not in j["runner"]


@pytest.mark.asyncio
async def test_list_compute_jobs_awaiting_local_runner_since(
    client: AsyncClient, db_session
):
    """A queued job whose task carries awaiting_local_runner_since must surface that
    field; when no runner has claimed it the runner descriptor must be null."""
    project = await _make_project(db_session, name="Awaiting Runner Project")
    await _add_member(db_session, project.id, "viewer")

    waiting_since = "2026-07-04T09:30:00Z"
    task = Task(
        project_id=project.id,
        title="queued compute task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"awaiting_local_runner_since": waiting_since},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.QUEUED,
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    j = resp.json()["compute_jobs"][0]

    assert j["awaiting_local_runner_since"] == waiting_since
    assert j["runner"] is None
    assert j["runner_target_id"] is None
    assert j["runner_claimed_at"] is None


@pytest.mark.asyncio
async def test_list_compute_jobs_no_runner_fields_null(client: AsyncClient, db_session):
    """A job with no runner pin must have all three runner fields null (backward-compat)."""
    project = await _make_project(db_session, name="No Runner Project")
    await _add_member(db_session, project.id, "viewer")
    _task, _job, _artifact = await _make_job(db_session, project)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    j = resp.json()["compute_jobs"][0]

    assert j["runner_target_id"] is None
    assert j["runner_claimed_at"] is None
    assert j["awaiting_local_runner_since"] is None
    assert j["runner"] is None


@pytest.mark.asyncio
async def test_get_compute_job_detail_surfaces_runner_claim(
    client: AsyncClient, db_session
):
    """The single-job detail endpoint must also surface runner fields."""
    project = await _make_project(db_session, name="Runner Detail Project")
    await _add_member(db_session, project.id, "viewer")
    runner = await _make_runner(db_session, project, hostname="detail-host")

    claimed_at = "2026-07-04T11:00:00Z"
    _task, job, _artifact = await _make_job(
        db_session,
        project,
        polling_metadata={
            "runner_target_id": str(runner.id),
            "runner_claimed_at": claimed_at,
        },
    )

    resp = await client.get(f"/api/admin/compute-jobs/{job.id}")
    assert resp.status_code == 200
    j = resp.json()["compute_job"]

    assert j["runner_target_id"] == str(runner.id)
    assert j["runner_claimed_at"] == claimed_at
    assert j["runner"]["hostname"] == "detail-host"


@pytest.mark.asyncio
async def test_compute_job_payload_exposes_launch_params(
    client: AsyncClient, db_session
):
    """launch_params must be projected from the task's first_payload fields so
    the operator UI can pre-fill the launch form from a finished job."""
    project = await _make_project(db_session, name="Launch Params Project")
    await _add_member(db_session, project.id, "viewer")
    task = Task(
        project_id=project.id,
        title="clone-me experiment",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "compute_experiment",
                "command": "python train.py",
                "declared_metric": "accuracy",
                "metric": "accuracy",
                "provenance": {
                    "idea": "Measure how the draft was shaped by the original operator idea",
                    "draft_notes": "The contract was refined from the draft before launch.",
                    "drafted_from_refinement": True,
                },
                "cwd": "/workspace",
                "env": {"SEED": "42", "LR": "0.01"},
                "accelerator": "T4",
                "machine_type": "n1-standard-1",
                "container_image": "nvcr.io/nvidia/pytorch:24.01",
                "provisioning_model": "SPOT",
                "spend_cap_usd": 5.0,
                "deadline_seconds": 3600,
                "require_live_release": True,
                "require_durable_artifact": True,
                "success_criterion": {
                    "metric": "accuracy",
                    "operator": ">=",
                    "threshold": 0.9,
                },
            }
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="n1-standard-1",
        spend_cap_usd=5.0,
        accrued_cost_usd=2.0,
        result_reference=None,
        polling_metadata={},
        completed_at=_utcnow(),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    j = resp.json()["compute_jobs"][0]
    lp = j["launch_params"]

    assert lp is not None
    assert lp["command"] == "python train.py"
    assert lp["declared_metric"] == "accuracy"
    assert lp["metric"] == "accuracy"
    assert lp["cwd"] == "/workspace"
    # Env values are operator-supplied secrets: only the sorted key names are
    # projected; the raw mapping must never reach this viewer-readable payload.
    assert lp["env_keys"] == ["LR", "SEED"]
    assert "env" not in lp
    assert "42" not in str(lp) and "0.01" not in str(lp)
    assert lp["provenance"] == {
        "idea": "Measure how the draft was shaped by the original operator idea",
        "draft_notes": "The contract was refined from the draft before launch.",
        "drafted_from_refinement": True,
    }
    assert lp["accelerator"] == "T4"
    assert lp["machine_type"] == "n1-standard-1"
    assert lp["container_image"] == "nvcr.io/nvidia/pytorch:24.01"
    assert lp["provisioning_model"] == "SPOT"
    assert lp["spend_cap_usd"] == 5.0
    assert lp["deadline_seconds"] == 3600
    assert lp["require_live_release"] is True
    assert lp["require_durable_artifact"] is True


@pytest.mark.asyncio
async def test_compute_job_payload_launch_params_arms_mode(
    client: AsyncClient, db_session
):
    """Arms-mode experiments must expose arms + declared_metric in launch_params."""
    project = await _make_project(db_session, name="Arms Launch Params Project")
    await _add_member(db_session, project.id, "viewer")
    arms = [
        {"name": "control", "command": "python run.py --lr 0.01"},
        {"name": "treatment", "command": "python run.py --lr 0.001"},
    ]
    task = Task(
        project_id=project.id,
        title="arms experiment",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "compute_experiment",
                "arms": arms,
                "declared_metric": "loss",
            }
        ],
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
        accrued_cost_usd=0.5,
        result_reference=None,
        polling_metadata={},
        completed_at=_utcnow(),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    lp = resp.json()["compute_jobs"][0]["launch_params"]

    assert lp["arms"] == arms
    assert lp["declared_metric"] == "loss"
    assert lp["command"] is None


@pytest.mark.asyncio
async def test_compute_job_launch_params_null_without_task(
    client: AsyncClient, db_session
):
    """A job with no linked task must expose launch_params as null."""
    project = await _make_project(db_session, name="No Task Project")
    await _add_member(db_session, project.id, "viewer")
    job = ComputeJob(
        task_id=None,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        accrued_cost_usd=0.1,
        result_reference=None,
        polling_metadata={},
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    assert resp.json()["compute_jobs"][0]["launch_params"] is None


async def _make_awaiting_local_runner_task(
    session,
    project,
    *,
    title="held compute task",
    awaiting_since="2026-07-04T10:00:00+00:00",
    created_at=None,
    payload=None,
    status=TaskStatus.QUEUED,
    requires_approval=False,
    archived=False,
    task_type="compute_experiment",
):
    """Seed a compute_experiment Task held awaiting a local runner (no ComputeJob)."""
    base_payload = {"type": task_type}
    if payload:
        base_payload.update(payload)
    task = Task(
        project_id=project.id,
        title=title,
        status=status,
        payloads=[base_payload],
        context={"awaiting_local_runner_since": awaiting_since}
        if awaiting_since is not None
        else {},
        requires_approval=requires_approval,
        archived=archived,
    )
    if created_at is not None:
        task.created_at = created_at
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_list_awaiting_local_runner_holds(client: AsyncClient, db_session):
    """Held compute_experiment tasks appear newest-first with stamp + launch fields;
    control tasks (approval-held, non-compute, unstamped, archived) are excluded."""
    project = await _make_project(db_session, name="Local Runner Hold Project")
    await _add_member(db_session, project.id, "viewer")

    older = await _make_awaiting_local_runner_task(
        db_session,
        project,
        title="older held experiment",
        awaiting_since="2026-07-04T09:00:00+00:00",
        created_at=datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc),
        payload={
            "spend_cap_usd": 3.5,
            "command": "python run_older.py",
            "machine_type": "a2-highgpu-1g",
            "accelerator": "nvidia-tesla-a100",
            "container_image": "img:older",
            "provisioning_model": "SPOT",
            "deadline_seconds": 1200,
        },
    )
    newer = await _make_awaiting_local_runner_task(
        db_session,
        project,
        title="newer held experiment",
        awaiting_since="2026-07-04T10:30:00+00:00",
        created_at=datetime(2026, 7, 4, 10, 30, 0, tzinfo=timezone.utc),
        payload={"spend_cap_usd": 5.0, "command": "python run_newer.py"},
    )

    # Controls that MUST be excluded.
    await _make_awaiting_local_runner_task(
        db_session,
        project,
        title="approval-held experiment",
        requires_approval=True,
    )
    await _make_awaiting_local_runner_task(
        db_session,
        project,
        title="non-compute task",
        task_type="research",
    )
    await _make_awaiting_local_runner_task(
        db_session,
        project,
        title="unstamped experiment",
        awaiting_since=None,
    )
    await _make_awaiting_local_runner_task(
        db_session,
        project,
        title="archived experiment",
        archived=True,
    )
    # A running experiment (has a ComputeJob, status IN_PROGRESS) must be excluded.
    await _make_job(db_session, project, status=ComputeJobStatus.RUNNING)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/compute-jobs/awaiting-local-runner"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert [entry["id"] for entry in body] == [str(newer.id), str(older.id)]

    newer_entry = body[0]
    assert newer_entry["title"] == "newer held experiment"
    assert newer_entry["awaiting_local_runner_since"] == "2026-07-04T10:30:00+00:00"
    assert newer_entry["created_at"] is not None
    assert newer_entry["requires_approval"] is False
    assert newer_entry["spend_cap_usd"] == 5.0
    assert newer_entry["command"] == "python run_newer.py"

    older_entry = body[1]
    assert older_entry["awaiting_local_runner_since"] == "2026-07-04T09:00:00+00:00"
    assert older_entry["spend_cap_usd"] == 3.5
    assert older_entry["command"] == "python run_older.py"
    assert older_entry["machine_type"] == "a2-highgpu-1g"
    assert older_entry["accelerator"] == "nvidia-tesla-a100"
    assert older_entry["container_image"] == "img:older"
    assert older_entry["provisioning_model"] == "SPOT"
    assert older_entry["deadline_seconds"] == 1200


@pytest.mark.asyncio
async def test_awaiting_local_runner_viewer_ok_non_member_404(
    client: AsyncClient, db_session
):
    """Viewer membership returns 200; no membership returns 404."""
    project = await _make_project(db_session, name="Hold Access Project")
    await _make_awaiting_local_runner_task(db_session, project)

    # No membership row -> 404.
    resp = await client.get(
        f"/api/admin/projects/{project.id}/compute-jobs/awaiting-local-runner"
    )
    assert resp.status_code == 404

    await _add_member(db_session, project.id, "viewer")
    resp = await client.get(
        f"/api/admin/projects/{project.id}/compute-jobs/awaiting-local-runner"
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_failed_job_response_has_first_class_exit_code_and_command_log(
    client: AsyncClient, db_session
):
    """exit_code and command_log must appear as first-class top-level fields on the
    admin compute-job response, not only inside the opaque polling_metadata bag.

    A terminal job whose polling_metadata carries exit_code (int) and command_log
    (dict) must expose them both as first-class keys at the root of the serialized
    job response, with the command_log already credential-redacted.  A job that
    carries no exit_code/command_log must still expose the keys (as null) so
    callers can rely on them being present without guarding for key absence.
    """
    project = await _make_project(db_session, name="First-Class Fields")
    await _add_member(db_session, project.id, "viewer")

    # Seed a failed job with exit_code and command_log in polling_metadata.
    _task, failed_job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {
            "exit_code": 1,
            "command_log": {
                "text": "running experiment\nANTHROPIC_API_KEY=sk-ant-api03-SECRET\nexit 1",
                "truncated": False,
                "byte_count": 70,
            },
            "result_metrics": {"accuracy": 0.42},
        },
    )
    failed_job.status = ComputeJobStatus.FAILED
    db_session.add(failed_job)
    await db_session.commit()

    # Seed a second job with no exit_code / command_log to confirm null promotion.
    _task2, empty_job = await _make_job_with_polling_metadata(
        db_session,
        project,
        {"result_metrics": {"accuracy": 0.9}},
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/compute-jobs")
    assert resp.status_code == 200
    jobs_by_id = {j["id"]: j for j in resp.json()["compute_jobs"]}

    # --- failed job ---
    failed = jobs_by_id[str(failed_job.id)]

    # exit_code must be a first-class integer field, not buried in polling_metadata.
    assert "exit_code" in failed, "exit_code must be a top-level field"
    assert failed["exit_code"] == 1

    # command_log must be a first-class dict field.
    assert "command_log" in failed, "command_log must be a top-level field"
    assert isinstance(failed["command_log"], dict)
    assert failed["command_log"]["truncated"] is False
    assert failed["command_log"]["byte_count"] == 70

    # The command_log text must have the credential redacted.
    log_text = failed["command_log"]["text"]
    assert "sk-ant-api03-SECRET" not in log_text, (
        "command_log text must be credential-redacted before promotion"
    )
    assert "running experiment" in log_text

    # --- job with no exit_code / command_log ---
    empty = jobs_by_id[str(empty_job.id)]
    assert "exit_code" in empty, "exit_code must always be present (null when absent)"
    assert empty["exit_code"] is None
    assert "command_log" in empty, "command_log must always be present (null when absent)"
    assert empty["command_log"] is None
