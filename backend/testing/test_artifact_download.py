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

import io
import tarfile
import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from app import compute_backends, storage
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobFile,
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

    mock_creds = MagicMock()
    mock_creds.service_account_email = "test-sa@example.iam.gserviceaccount.com"
    mock_creds.token = "fake_token"

    with (
        patch("google.auth.default", return_value=(mock_creds, "test-project")),
        patch("google.auth.transport.requests.Request"),
    ):
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
    mock_creds = MagicMock()
    mock_creds.service_account_email = "test-sa@example.iam.gserviceaccount.com"
    mock_creds.token = "fake_token"
    mocker.patch("google.auth.default", return_value=(mock_creds, "test-project"))
    mocker.patch("google.auth.transport.requests.Request")
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


# --------------------------------------------------------------------------
# Viewable per-file artifact substrate
# --------------------------------------------------------------------------

# Minimal magic-byte-prefixed blobs so the payloads look plausible; extension
# classification is what actually drives selection.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fake-mp4-body"


def _make_tar_gz_bundle(files: dict[str, bytes]) -> bytes:
    """Build an in-memory ``.tar.gz`` whose members mirror ``tar -C dir -czf - .``
    (member names like ``./plot.png``)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ---- pure helper tests (no DB) ----


def test_viewable_content_type_classifies_images_and_videos():
    assert storage.viewable_content_type_for_filename("plot.png") == ("image/png", "image")
    assert storage.viewable_content_type_for_filename("a.JPG") == ("image/jpeg", "image")
    assert storage.viewable_content_type_for_filename("clip.mp4") == ("video/mp4", "video")
    assert storage.viewable_content_type_for_filename("v.webm") == ("video/webm", "video")
    # Non-viewable / no extension.
    assert storage.viewable_content_type_for_filename("data.bin") is None
    assert storage.viewable_content_type_for_filename("noext") is None


def test_svg_excluded_fail_closed_on_both_viewable_surfaces():
    """Finding 63ee179fc66a7b97: the per-file viewable substrate and the
    bundle-extraction surface MUST agree on SVG, and the agreed policy is
    fail-closed exclusion (an SVG served from a signed URL can execute script
    in the viewer's browsing context)."""
    # Surface 1: the individually-addressable viewable substrate.
    assert storage.viewable_content_type_for_filename("chart.svg") is None
    assert "svg" not in storage._VIEWABLE_CONTENT_TYPES
    # Surface 2: the durable-bundle extraction allowlist.
    assert ".svg" not in storage.VIEWABLE_COMPUTE_JOB_EXTENSIONS
    # Both surfaces agree, and they agree on exclusion.
    assert storage.svg_is_viewable() is False


def test_sanitize_viewable_basename_strips_traversal():
    assert storage.sanitize_viewable_basename("../../etc/passwd.png") == "passwd.png"
    assert storage.sanitize_viewable_basename("sub/dir/fig 1.png") == "fig_1.png"
    assert storage.sanitize_viewable_basename("evil/../x.png") == "x.png"
    # Degenerate names.
    assert storage.sanitize_viewable_basename("..") is None
    assert storage.sanitize_viewable_basename("/") is None
    assert storage.sanitize_viewable_basename("") is None


def test_compute_job_viewable_uri_lives_under_allowlisted_prefix():
    jid = uuid.uuid4()
    uri = storage.compute_job_viewable_uri(jid, "plot.png")
    assert uri == f"gs://{BUCKET}/compute-jobs/{jid}/artifacts/viewable/plot.png"
    # The existing compute-job authorization binds it to the owning job.
    assert storage.compute_job_id_for_artifact_uri(uri) == jid


def test_extract_viewable_candidates_selects_image_and_video():
    bundle = _make_tar_gz_bundle(
        {
            "./plot.png": _PNG_BYTES,
            "./clip.mp4": _MP4_BYTES,
            "./notes.txt": b"ignore me",  # non-viewable
        }
    )
    cands = compute_backends._extract_viewable_candidates_from_bundle(bundle)
    by_name = {c.filename: c for c in cands}
    assert set(by_name) == {"plot.png", "clip.mp4"}
    assert by_name["plot.png"].kind == "image"
    assert by_name["plot.png"].content_type == "image/png"
    assert by_name["plot.png"].data == _PNG_BYTES
    assert by_name["plot.png"].size_bytes == len(_PNG_BYTES)
    assert by_name["clip.mp4"].kind == "video"
    assert by_name["clip.mp4"].content_type == "video/mp4"


def test_extract_viewable_candidates_excludes_nonviewable_and_oversized(monkeypatch):
    monkeypatch.setattr(compute_backends, "_MAX_VIEWABLE_FILE_BYTES", 16)
    bundle = _make_tar_gz_bundle(
        {
            "./huge.png": b"x" * 64,  # 64 bytes > 16-byte cap -> excluded
            "./data.bin": b"binary-blob",  # non-viewable -> excluded
            "./ok.webp": b"tiny",  # 4 bytes -> kept
        }
    )
    cands = compute_backends._extract_viewable_candidates_from_bundle(bundle)
    assert [c.filename for c in cands] == ["ok.webp"]
    assert cands[0].content_type == "image/webp"
    assert cands[0].kind == "image"


@pytest.mark.asyncio
async def test_extract_viewable_candidates_enforces_total_count_cap(monkeypatch, mocker):
    """The total-count cap is enforced by ``_collect_viewable_artifact_candidates``,
    NOT by the bundle extractor (which returns every qualifying file so the caller
    can sort deterministically and report a dropped count). A bundle with more
    viewable files than the cap yields exactly ``cap`` selected candidates, with the
    remainder surfaced as the dropped count."""
    monkeypatch.setattr(compute_backends, "_MAX_VIEWABLE_FILE_COUNT", 2)
    bundle = _make_tar_gz_bundle(
        {f"./img{i}.png": _PNG_BYTES for i in range(5)}
    )
    # The extractor itself does not cap — it returns all 5 qualifying files.
    assert (
        len(compute_backends._extract_viewable_candidates_from_bundle(bundle)) == 5
    )
    mocker.patch(
        "app.storage.download_compute_job_artifact_bundle", return_value=bundle
    )

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends.GCP_BACKEND_NAME

    selected, dropped = await compute_backends._collect_viewable_artifact_candidates(job)
    assert len(selected) == 2
    assert dropped == 3


def test_extract_viewable_candidates_corrupt_or_empty_bundle_returns_empty():
    assert compute_backends._extract_viewable_candidates_from_bundle(b"") == []
    assert (
        compute_backends._extract_viewable_candidates_from_bundle(b"not-a-gzip-tar")
        == []
    )


def test_viewable_artifacts_descriptor_sanitizes_manifest():
    from app.routers.admin_compute_jobs import _viewable_artifacts_descriptor

    # Absent / malformed manifests degrade to an empty list.
    assert _viewable_artifacts_descriptor(None) == []
    assert _viewable_artifacts_descriptor("nope") == []
    assert _viewable_artifacts_descriptor([{"filename": "x"}]) == []  # no storage_uri

    out = _viewable_artifacts_descriptor(
        [
            {
                "filename": "plot.png",
                "storage_uri": "gs://b/compute-jobs/j/artifacts/viewable/plot.png",
                "content_type": "image/png",
                "size_bytes": 128,
                "kind": "image",
            },
            "not-a-dict",
            {"storage_uri": "", "kind": "image"},  # empty uri dropped
            {
                "filename": None,
                "storage_uri": "gs://b/compute-jobs/j/artifacts/viewable/v.mp4",
                "content_type": 5,  # wrong type -> None
                "size_bytes": "big",  # wrong type -> None
                "kind": "bogus",  # not image/video -> None
            },
        ]
    )
    assert len(out) == 2
    assert out[0] == {
        "filename": "plot.png",
        "storage_uri": "gs://b/compute-jobs/j/artifacts/viewable/plot.png",
        "content_type": "image/png",
        "size_bytes": 128,
        "kind": "image",
    }
    assert out[1] == {
        "filename": None,
        "storage_uri": "gs://b/compute-jobs/j/artifacts/viewable/v.mp4",
        "content_type": None,
        "size_bytes": None,
        "kind": None,
    }


# ---- persist orchestration (mocked storage) ----


async def _make_bundle_backed_job(session, project, task, backend="gcp"):
    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend=backend,
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_persist_viewable_artifacts_builds_manifest(db_session, mocker):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    job = await _make_bundle_backed_job(db_session, project, task)

    bundle = _make_tar_gz_bundle(
        {"./plot.png": _PNG_BYTES, "./clip.mp4": _MP4_BYTES, "./log.txt": b"skip"}
    )
    mocker.patch("app.storage.download_compute_job_artifact_bundle", return_value=bundle)
    # Real upload helper runs against a mocked GCS client (no network).
    gcs_client = mocker.patch("app.storage.storage.Client")
    blob = gcs_client.return_value.bucket.return_value.blob.return_value

    manifest, dropped = await compute_backends.persist_viewable_artifacts(job)

    assert dropped == 0  # 2 viewable files, well under the count cap
    by_name = {m["filename"]: m for m in manifest}
    assert set(by_name) == {"plot.png", "clip.mp4"}
    assert by_name["plot.png"]["kind"] == "image"
    assert by_name["plot.png"]["content_type"] == "image/png"
    assert by_name["plot.png"]["size_bytes"] == len(_PNG_BYTES)
    assert by_name["plot.png"]["storage_uri"] == storage.compute_job_viewable_uri(
        job.id, "plot.png"
    )
    assert by_name["clip.mp4"]["kind"] == "video"
    assert by_name["clip.mp4"]["content_type"] == "video/mp4"
    # Each viewable file uploaded once, with its content_type.
    assert blob.upload_from_string.call_count == 2
    content_types = {
        call.kwargs.get("content_type")
        for call in blob.upload_from_string.call_args_list
    }
    assert content_types == {"image/png", "video/mp4"}


@pytest.mark.asyncio
async def test_persist_viewable_artifacts_corrupt_bundle_degrades(db_session, mocker):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    job = await _make_bundle_backed_job(db_session, project, task, backend="runner-subprocess")

    mocker.patch(
        "app.storage.download_compute_job_artifact_bundle",
        return_value=b"corrupt-not-a-tar",
    )
    upload = mocker.patch("app.storage.upload_compute_job_viewable_file")

    manifest, dropped = await compute_backends.persist_viewable_artifacts(job)
    assert (manifest, dropped) == ([], 0)
    upload.assert_not_called()


@pytest.mark.asyncio
async def test_persist_viewable_artifacts_missing_bundle_degrades(db_session, mocker):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project)
    job = await _make_bundle_backed_job(db_session, project, task)

    mocker.patch("app.storage.download_compute_job_artifact_bundle", return_value=None)
    upload = mocker.patch("app.storage.upload_compute_job_viewable_file")

    manifest, dropped = await compute_backends.persist_viewable_artifacts(job)
    assert (manifest, dropped) == ([], 0)
    upload.assert_not_called()


# ---- download authorization for viewable objects ----


async def _make_compute_job_with_viewable(session, project, task):
    """Compute job whose polling_metadata carries a viewable-file manifest."""
    job = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="gcp",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
    )
    session.add(job)
    await session.flush()
    viewable_uri = f"gs://{BUCKET}/compute-jobs/{job.id}/artifacts/viewable/plot.png"
    job.polling_metadata = {
        "viewable_files": [
            {
                "filename": "plot.png",
                "storage_uri": viewable_uri,
                "content_type": "image/png",
                "size_bytes": 42,
                "kind": "image",
            }
        ]
    }
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job, viewable_uri


@pytest.mark.asyncio
async def test_resolve_download_url_viewable_artifact_ok(
    client: AsyncClient, db_session, mocker
):
    """A URI recorded in the job's viewable-file manifest is signable."""
    _mock_signing(mocker, url="https://signed.example/viewable-ok")
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    _job, viewable_uri = await _make_compute_job_with_viewable(db_session, project, task)

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": viewable_uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://signed.example/viewable-ok"


@pytest.mark.asyncio
async def test_resolve_download_url_viewable_sibling_not_in_manifest_404(
    client: AsyncClient, db_session, mocker
):
    """A viewable-prefix sibling NOT recorded in the manifest stays 404."""
    sign = _mock_signing(mocker)
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    job, viewable_uri = await _make_compute_job_with_viewable(db_session, project, task)

    sibling = f"gs://{BUCKET}/compute-jobs/{job.id}/artifacts/viewable/secret.png"
    assert sibling != viewable_uri

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": sibling, "project_id": str(project.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()


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


# ---------------------------------------------------------------------------
# ComputeJobFile media-file tests
# ---------------------------------------------------------------------------


async def _make_compute_job_file(
    session, job: ComputeJob, relpath: str = "plot.png", media_type: str = "image"
) -> ComputeJobFile:
    """Create a ComputeJobFile row for a viewable file under a compute job's prefix."""
    storage_uri = f"gs://{BUCKET}/compute-jobs/{job.id}/artifacts/files/{relpath}"
    cjf = ComputeJobFile(
        compute_job_id=job.id,
        safe_relpath=relpath,
        media_type=media_type,
        storage_uri=storage_uri,
        content_type="image/png" if media_type == "image" else "video/mp4",
    )
    session.add(cjf)
    await session.commit()
    await session.refresh(cjf)
    return cjf


@pytest.mark.asyncio
async def test_resolve_download_url_compute_job_media_file_ok(
    client: AsyncClient, db_session, mocker
):
    """A ComputeJobFile storage_uri is accepted by the download URL endpoint."""
    _mock_signing(mocker, url="https://signed.example/media-ok")
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    job = await _make_compute_job(db_session, project, task)
    cjf = await _make_compute_job_file(db_session, job, relpath="plot.png")

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": cjf.storage_uri, "project_id": str(project.id)},
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://signed.example/media-ok"


@pytest.mark.asyncio
async def test_resolve_download_url_compute_job_media_file_wrong_job_404(
    client: AsyncClient, db_session, mocker
):
    """A ComputeJobFile whose compute_job_id doesn't match the URI's embedded
    job UUID is rejected as 404."""
    sign = _mock_signing(mocker)
    project = await _make_project(db_session)
    await _add_member(db_session, project.id)
    task = await _make_task(db_session, project)
    job_a = await _make_compute_job(db_session, project, task)
    job_b = await _make_compute_job(db_session, project, task)

    # Create a ComputeJobFile whose storage_uri is under job_a's prefix
    # but whose compute_job_id points to job_b.
    uri_under_a = f"gs://{BUCKET}/compute-jobs/{job_a.id}/artifacts/files/result.png"
    cjf = ComputeJobFile(
        compute_job_id=job_b.id,
        safe_relpath="result.png",
        media_type="image",
        storage_uri=uri_under_a,
    )
    db_session.add(cjf)
    await db_session.commit()

    resp = await client.get(
        "/api/admin/artifacts/download-url",
        params={"storage_uri": uri_under_a, "project_id": str(project.id)},
    )
    assert resp.status_code == 404
    sign.assert_not_called()
