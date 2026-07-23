"""
Tests for GCS artifact IAM helpers and signed-URL generation.

Covers:
- compute_job_artifact_uri / prefix computation
- generate_compute_job_artifact_upload_signed_url (PUT signed URL)
- _compute_job_artifact_condition_expression (prefix-scoped IAM condition)
- grant_compute_job_artifact_writer / revoke_compute_job_artifact_writer
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import storage
# ---------------------------------------------------------------------------
# Storage artifact IAM / signed-URL helpers
# ---------------------------------------------------------------------------


class _FakeIamPolicy:
    """Minimal stand-in for the storage IAM Policy used by the artifact grant."""

    def __init__(self, bindings=None, version=1):
        self.bindings = list(bindings or [])
        self.version = version


class _FakeBucket:
    def __init__(self, policy):
        self._policy = policy
        self.set_calls = []

    def get_iam_policy(self, requested_policy_version=1):
        return self._policy

    def set_iam_policy(self, policy):
        self.set_calls.append(policy)
        self._policy = policy


class _FakeStorageClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, _name):
        return self._bucket


def test_compute_job_artifact_uri_and_prefix_are_per_job():
    job_id = uuid.uuid4()
    prefix = storage.compute_job_artifact_object_prefix(job_id)
    assert prefix == f"compute-jobs/{job_id}/artifacts"
    assert storage.compute_job_artifact_uri(job_id) == (
        f"gs://{storage.ARTIFACT_BUCKET_NAME}/{prefix}/bundle.tar.gz"
    )


@pytest.mark.asyncio
async def test_compute_job_artifact_upload_signed_url_targets_bundle_object(
    monkeypatch,
):
    job_id = uuid.uuid4()
    expected_object = storage.compute_job_artifact_object_name(job_id)
    expires_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    blob = MagicMock()
    blob.generate_signed_url.return_value = "https://storage.example.com/upload"
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch("google.cloud.storage.Client", return_value=client):
        url = await storage.generate_compute_job_artifact_upload_signed_url(
            job_id, expires_at=expires_at
        )

    assert url == "https://storage.example.com/upload"
    client.bucket.assert_called_once_with(storage.ARTIFACT_BUCKET_NAME)
    bucket.blob.assert_called_once_with(expected_object)
    blob.generate_signed_url.assert_called_once()
    call_kwargs = blob.generate_signed_url.call_args.kwargs
    assert call_kwargs["version"] == "v4"
    assert call_kwargs["method"] == "PUT"
    assert call_kwargs["content_type"] == "application/gzip"
    assert call_kwargs["expiration"] == expires_at


def test_artifact_condition_scopes_writes_to_one_job_prefix_only():
    job_id = uuid.uuid4()
    expression = storage._compute_job_artifact_condition_expression(job_id)
    expected_prefix = (
        f"projects/_/buckets/{storage.ARTIFACT_BUCKET_NAME}/objects/"
        f"compute-jobs/{job_id}/artifacts/"
    )
    assert expression == f'resource.name.startsWith("{expected_prefix}")'
    # Never the whole bucket: a bare bucket-root prefix must not appear.
    assert (
        f'resource.name.startsWith("projects/_/buckets/{storage.ARTIFACT_BUCKET_NAME}/objects/")'
        not in expression
    )


def test_artifact_condition_includes_self_expiry_when_given():
    job_id = uuid.uuid4()
    expires_at = datetime(2026, 6, 7, 12, 0, 0)
    expression = storage._compute_job_artifact_condition_expression(job_id, expires_at)
    assert 'request.time < timestamp("2026-06-07T12:00:00Z")' in expression


@pytest.mark.asyncio
async def test_grant_artifact_writer_adds_prefix_scoped_binding(monkeypatch):
    job_id = uuid.uuid4()
    bucket = _FakeBucket(_FakeIamPolicy())
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: _FakeStorageClient(bucket)
    )

    ok = await storage.grant_compute_job_artifact_writer(
        job_id, member="serviceAccount:vpm@example.iam.gserviceaccount.com"
    )

    assert ok is True
    assert len(bucket.set_calls) == 1
    binding = bucket._policy.bindings[-1]
    assert binding["role"] == "roles/storage.objectAdmin"
    assert binding["members"] == {"serviceAccount:vpm@example.iam.gserviceaccount.com"}
    assert str(job_id) in binding["condition"]["expression"]
    assert binding["condition"]["title"] == (
        storage._compute_job_artifact_condition_title(job_id)
    )


@pytest.mark.asyncio
async def test_grant_artifact_writer_replaces_stale_binding_for_same_job(monkeypatch):
    job_id = uuid.uuid4()
    title = storage._compute_job_artifact_condition_title(job_id)
    stale = {
        "role": "roles/storage.objectAdmin",
        "members": {"serviceAccount:old@example.iam.gserviceaccount.com"},
        "condition": {"title": title, "expression": "stale"},
    }
    other = {"role": "roles/storage.objectViewer", "members": {"user:keep@example.com"}}
    bucket = _FakeBucket(_FakeIamPolicy(bindings=[stale, other]))
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: _FakeStorageClient(bucket)
    )

    await storage.grant_compute_job_artifact_writer(
        job_id, member="serviceAccount:new@example.iam.gserviceaccount.com"
    )

    titled = [
        b
        for b in bucket._policy.bindings
        if (b.get("condition") or {}).get("title") == title
    ]
    assert len(titled) == 1
    assert titled[0]["members"] == {
        "serviceAccount:new@example.iam.gserviceaccount.com"
    }
    # Unrelated bindings are preserved.
    assert other in bucket._policy.bindings


@pytest.mark.asyncio
async def test_revoke_artifact_writer_removes_only_its_binding(monkeypatch):
    job_id = uuid.uuid4()
    title = storage._compute_job_artifact_condition_title(job_id)
    mine = {
        "role": "roles/storage.objectAdmin",
        "members": {"serviceAccount:vpm@example.iam.gserviceaccount.com"},
        "condition": {"title": title, "expression": "expr"},
    }
    other = {"role": "roles/storage.objectViewer", "members": {"user:keep@example.com"}}
    bucket = _FakeBucket(_FakeIamPolicy(bindings=[mine, other]))
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: _FakeStorageClient(bucket)
    )

    ok = await storage.revoke_compute_job_artifact_writer(
        job_id, member="serviceAccount:vpm@example.iam.gserviceaccount.com"
    )

    assert ok is True
    assert mine not in bucket._policy.bindings
    assert other in bucket._policy.bindings


@pytest.mark.asyncio
async def test_grant_artifact_writer_is_best_effort_on_error(monkeypatch):
    def _boom(project=None):
        raise RuntimeError("iam down")

    monkeypatch.setattr(storage.storage, "Client", _boom)
    ok = await storage.grant_compute_job_artifact_writer(
        uuid.uuid4(), member="serviceAccount:vpm@example.iam.gserviceaccount.com"
    )
    assert ok is False


