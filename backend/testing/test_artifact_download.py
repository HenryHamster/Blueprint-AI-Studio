"""Tests for the signed artifact download resolver.

Two layers:
* Pure helpers in ``app.storage`` — ``parse_artifact_uri`` /
  ``task_id_for_artifact_uri`` enforce the gs://-scheme + artifact-bucket
  allowlist and bind a reference to its owning task. No DB / GCS needed.
* The ``GET /api/admin/artifacts/download-url`` endpoint — authorizes the
  caller for the reference's owning project and fails closed on malformed,
  out-of-bucket, cross-project, and storage-failure inputs.

GCS signing is mocked at ``app.storage.storage.Client`` so the real validation
path runs but no network/credentials are required.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from app import storage
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobStatus,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
)

TEST_USER_ID = "test-user-123"
BUCKET = storage.ARTIFACT_BUCKET_NAME


# --------------------------------------------------------------------------
# Pure helper tests (no DB)
# --------------------------------------------------------------------------


def test_parse_artifact_uri_accepts_in_bucket_object():
    tid = uuid.uuid4()
    bucket, obj = storage.parse_artifact_uri(f"gs://{BUCKET}/tasks/{tid}/out.json")
    assert bucket == BUCKET
    assert obj == f"tasks/{tid}/out.json"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-uri",
        "http://evil.example.com/tasks/x",  # non-gs scheme
        "https://storage.googleapis.com/blueprint-agent-artifacts/o",
        f"gs://some-other-bucket/tasks/{uuid.uuid4()}/o.json",  # out-of-bucket
        f"gs://{BUCKET}/",  # no object
        f"gs://{BUCKET}/a/../b",  # parent-directory traversal
        f"gs://{BUCKET}/has space/o",  # disallowed char
    ],
)
def test_parse_artifact_uri_rejects_bad_references(bad):
    with pytest.raises(storage.StorageUriError):
        storage.parse_artifact_uri(bad)


def test_parse_artifact_uri_rejects_non_string():
    with pytest.raises(storage.StorageUriError):
        storage.parse_artifact_uri(None)  # type: ignore[arg-type]


def test_task_id_for_artifact_uri_extracts_owning_task():
    tid = uuid.uuid4()
    assert storage.task_id_for_artifact_uri(f"gs://{BUCKET}/tasks/{tid}/out.json") == tid


def test_task_id_for_artifact_uri_none_for_non_task_or_invalid():
    # Valid bucket object but not under a tasks/<uuid>/ prefix.
    assert (
        storage.task_id_for_artifact_uri(f"gs://{BUCKET}/viewport-failures/x/y.png")
        is None
    )
    # Out-of-bucket reference can't be bound to a task at all.
    assert storage.task_id_for_artifact_uri("gs://other/tasks/x/o") is None


def test_compute_job_id_for_artifact_uri_extracts_owning_job():
    jid = uuid.uuid4()
    assert (
        storage.compute_job_id_for_artifact_uri(
            f"gs://{BUCKET}/compute-jobs/{jid}/artifacts/bundle.tar.gz"
        )
        == jid
    )


def test_compute_job_id_for_artifact_uri_none_for_non_job_or_invalid():
    assert (
        storage.compute_job_id_for_artifact_uri(
            f"gs://{BUCKET}/tasks/{uuid.uuid4()}/o.json"
        )
        is None
    )
    assert storage.compute_job_id_for_artifact_uri("gs://other/compute-jobs/x/o") is None


@pytest.mark.asyncio
async def test_generate_download_signed_url_validates_before_signing(mocker):
    """A bad reference raises StorageUriError without ever touching GCS."""
    client = mocker.patch("app.storage.storage.Client")
    with pytest.raises(storage.StorageUriError):
        await storage.generate_download_signed_url("gs://other-bucket/o")
    client.assert_not_called()


@pytest.mark.asyncio
async def test_generate_download_signed_url_signs_valid_reference(mocker):
    blob = MagicMock()
    blob.generate_signed_url.return_value = "https://signed.example/get"
    client = mocker.patch("app.storage.storage.Client")
    client.return_value.bucket.return_value.blob.return_value = blob

    url = await storage.generate_download_signed_url(
        f"gs://{BUCKET}/tasks/{uuid.uuid4()}/o.json"
    )
    assert url == "https://signed.example/get"
    # V4 GET signature, not PUT.
    _, kwargs = blob.generate_signed_url.call_args
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "GET"


# --------------------------------------------------------------------------
# Endpoint tests (DB-backed)
# --------------------------------------------------------------------------


async def _make_project(session, name="Artifact Project"):
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _add_member(session, project_id, role="member"):
    session.add(ProjectMember(project_id=project_id, user_id=TEST_USER_ID, role=role))
    await session.commit()


async def _make_task(session, project):
    task = Task(
        project_id=project.id,
        title="artifact task",
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
        f"gs://{BUCKET}/compute-jobs/{job.id}/artifacts/bundle.tar.gz"
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def _make_compute_job_no_result(session, project, task):
    """Compute job with neither result_reference nor artifact_id."""
    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def _make_compute_job_with_artifact(session, project, task):
    """Compute job whose terminal output is recorded via a linked Artifact row."""
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.SCREENSHOT,
        storage_uri=None,  # filled in after job id is known
    )
    session.add(artifact)
    await session.flush()

    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        artifact_id=artifact.id,
    )
    session.add(job)
    await session.flush()

    artifact.storage_uri = (
        f"gs://{BUCKET}/compute-jobs/{job.id}/artifacts/output.tar.gz"
    )
    session.add(artifact)
    await session.commit()
    await session.refresh(job)
    await session.refresh(artifact)
    return job, artifact


def _mock_signing(mocker, *, url="https://signed.example/download", raises=None):
    client = mocker.patch("app.storage.storage.Client")
    blob = client.return_value.bucket.return_value.blob.return_value
    if raises is not None:
        blob.generate_signed_url.side_effect = raises
    else:
        blob.generate_signed_url.return_value = url
    return client


@pytest.mark.asyncio
async def test_resolve_download_url_ok(client: AsyncClient, db_session, mocker):
    _mock_signing(mocker, url="https://signed.example/ok")
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    uri = f"gs://{BUCKET}/tasks/{task.id}/result.json"

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://signed.example/ok"


@pytest.mark.asyncio
async def test_resolve_download_url_malformed_400(client: AsyncClient, db_session, mocker):
    sign = _mock_signing(mocker)
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": "http://evil/x", "project_id": str(project.id)},
    )
    assert resp.status_code == 400
    # Never reached the signing layer.
    sign.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_download_url_out_of_bucket_400(
    client: AsyncClient, db_session, mocker
):
    _mock_signing(mocker)
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    uri = f"gs://some-other-bucket/tasks/{uuid.uuid4()}/o.json"

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_resolve_download_url_cross_project_404(
    client: AsyncClient, db_session, mocker
):
    """A reference whose owning task is in project B can't be resolved through
    project A even when the caller is a member of both."""
    _mock_signing(mocker)
    project_a = await _make_project(db_session, name="A")
    project_b = await _make_project(db_session, name="B")
    await _add_member(db_session, project_a.id)
    await _add_member(db_session, project_b.id)
    task_b = await _make_task(db_session, project_b)
    uri = f"gs://{BUCKET}/tasks/{task_b.id}/o.json"

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project_a.id)},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_download_url_unauthorized_project_404(
    client: AsyncClient, db_session, mocker
):
    """The caller is not a member of the reference's owning project."""
    _mock_signing(mocker)
    project = await _make_project(db_session, name="NoMember")
    # No ProjectMember row for the test user.
    task = await _make_task(db_session, project)
    uri = f"gs://{BUCKET}/tasks/{task.id}/o.json"

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_download_url_non_task_path_404(
    client: AsyncClient, db_session, mocker
):
    """A valid in-bucket object that isn't under tasks/<uuid>/ can't be
    authorized by task ownership, so it is rejected."""
    _mock_signing(mocker)
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    uri = f"gs://{BUCKET}/viewport-failures/{project.id}/comp-375.png"

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_download_url_compute_job_reference_ok(
    client: AsyncClient, db_session, mocker
):
    _mock_signing(mocker, url="https://signed.example/job-ok")
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    job = await _make_compute_job(db_session, project, task)

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": job.result_reference, "project_id": str(project.id)},
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://signed.example/job-ok"


@pytest.mark.asyncio
async def test_resolve_download_url_storage_failure_503(
    client: AsyncClient, db_session, mocker
):
    _mock_signing(mocker, raises=RuntimeError("signing key unavailable"))
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    uri = f"gs://{BUCKET}/tasks/{task.id}/o.json"

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_compute_job_download_rejects_unlinked_prefix_object(
    client: AsyncClient, db_session, mocker
):
    """A URI under compute-jobs/<job_id>/ that is NOT the recorded
    result_reference is rejected as 404 even when the caller has project
    membership — unknown siblings are indistinguishable from not found."""
    sign = _mock_signing(mocker, url="https://signed.example/should-not-reach")
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    job = await _make_compute_job(db_session, project, task)

    # A sibling object under the same job prefix that was never recorded as
    # the terminal output.
    sibling_uri = f"gs://{BUCKET}/compute-jobs/{job.id}/artifacts/secret.bin"
    assert sibling_uri != job.result_reference

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": sibling_uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_download_url_compute_job_artifact_storage_uri_ok(
    client: AsyncClient, db_session, mocker
):
    """When the job's terminal output is recorded via artifact_id, requesting
    the artifact's storage_uri must succeed."""
    _mock_signing(mocker, url="https://signed.example/artifact-ok")
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    job, artifact = await _make_compute_job_with_artifact(db_session, project, task)

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": artifact.storage_uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://signed.example/artifact-ok"


@pytest.mark.asyncio
async def test_resolve_download_url_compute_job_no_result_reference_404(
    client: AsyncClient, db_session, mocker
):
    """A job with no result_reference and no artifact_id: any URI under its
    prefix is rejected as 404."""
    sign = _mock_signing(mocker)
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    job = await _make_compute_job_no_result(db_session, project, task)

    uri = f"gs://{BUCKET}/compute-jobs/{job.id}/artifacts/bundle.tar.gz"

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()
