"""Google Cloud Storage helpers for agent task payloads and artifacts.

Wraps the blocking ``google.cloud.storage`` client in async helpers (run in a
thread) for uploading large task payloads to the artifact bucket as
``gs://`` URIs and for minting V4 signed upload URLs the agent VM can use to
push durable compute-job artifact bundles back. The bucket and GCP project are
read from the ``ARTIFACT_BUCKET_NAME`` / ``GCP_PROJECT_ID`` environment
variables.
"""

import os
import datetime
import logging
import asyncio
import json
from google.cloud import storage

logger = logging.getLogger(__name__)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project-id")
ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")

# Object-name root under the shared artifact bucket where each compute job's
# durable outputs live. Each job uploads one durable bundle under
# ``<root>/<job_id>/artifacts/``; the prefix is the storage layout, not an IAM
# boundary.
COMPUTE_JOB_ARTIFACT_ROOT = "compute-jobs"
_COMPUTE_JOB_ARTIFACT_BUNDLE_NAME = "bundle.tar.gz"
# Legacy IAM role helpers are kept for compatibility with older tests and
# call sites, but compute-job uploads now use signed URLs instead of a shared SA.
_COMPUTE_JOB_ARTIFACT_ROLE = "roles/storage.objectAdmin"


def compute_job_artifact_object_prefix(job_id) -> str:
    """Object-name prefix (no bucket) for one compute job's durable artifacts."""
    return f"{COMPUTE_JOB_ARTIFACT_ROOT}/{job_id}/artifacts"


def compute_job_artifact_object_name(job_id) -> str:
    """Object-name (no bucket) for one compute job's durable artifact bundle."""
    return f"{compute_job_artifact_object_prefix(job_id)}/{_COMPUTE_JOB_ARTIFACT_BUNDLE_NAME}"


def compute_job_artifact_uri(job_id) -> str:
    """``gs://`` URI of one compute job's durable artifact bundle."""
    return f"gs://{ARTIFACT_BUCKET_NAME}/{compute_job_artifact_object_name(job_id)}"


async def generate_compute_job_artifact_upload_signed_url(
    job_id, *, expires_at: datetime.datetime | None = None
) -> str:
    """Generate a signed upload URL for one compute job's artifact bundle."""

    object_name = compute_job_artifact_object_name(job_id)
    logger.info(
        "Generating compute-job artifact upload signed URL for gs://%s/%s",
        ARTIFACT_BUCKET_NAME,
        object_name,
    )

    def _generate():
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        expiration = expires_at or (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=2)
        )
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=datetime.timezone.utc)
        return blob.generate_signed_url(
            version="v4",
            expiration=expiration,
            method="PUT",
            content_type="application/gzip",
        )

    try:
        return await asyncio.to_thread(_generate)
    except Exception:
        logger.exception("Failed to generate compute-job artifact upload signed URL")
        raise


def _compute_job_artifact_condition_title(job_id) -> str:
    """Stable IAM-condition title so a binding can be re-found for revoke."""
    return f"blueprint-compute-job-artifacts-{job_id}"


def _compute_job_artifact_condition_expression(job_id, expires_at=None) -> str:
    """CEL expression scoping a write binding to one job's object prefix.

    Optionally AND-ed with a ``request.time`` upper bound so the grant
    self-expires even if the explicit revoke never runs (e.g. the VM is
    preempted before teardown).
    """
    prefix = (
        f"projects/_/buckets/{ARTIFACT_BUCKET_NAME}/objects/"
        f"{compute_job_artifact_object_prefix(job_id)}/"
    )
    expression = f'resource.name.startsWith("{prefix}")'
    if expires_at is not None:
        iso = (
            expires_at.astimezone(datetime.timezone.utc)
            if expires_at.tzinfo is not None
            else expires_at.replace(tzinfo=datetime.timezone.utc)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        expression = f'{expression} && request.time < timestamp("{iso}")'
    return expression


async def grant_compute_job_artifact_writer(
    job_id, *, member: str, expires_at=None
) -> bool:
    """Grant ``member`` prefix-scoped write access to one job's artifact namespace.

    Adds a conditional IAM binding to the shared artifact bucket whose condition
    restricts writes to ``<root>/<job_id>/artifacts/`` only — never the whole
    bucket. Idempotent: a stale binding for the same job (by condition title) is
    replaced. Best-effort by contract: returns ``False`` on any error instead of
    raising, so a launch is never aborted by an IAM hiccup (the on-VM upload is
    itself best-effort).
    """

    title = _compute_job_artifact_condition_title(job_id)
    expression = _compute_job_artifact_condition_expression(job_id, expires_at)

    def _grant() -> bool:
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        policy = bucket.get_iam_policy(requested_policy_version=3)
        policy.version = 3
        policy.bindings = [
            binding
            for binding in policy.bindings
            if (binding.get("condition") or {}).get("title") != title
        ]
        policy.bindings.append(
            {
                "role": _COMPUTE_JOB_ARTIFACT_ROLE,
                "members": {member},
                "condition": {
                    "title": title,
                    "description": "Blueprint compute-job durable artifacts (prefix-scoped)",
                    "expression": expression,
                },
            }
        )
        bucket.set_iam_policy(policy)
        return True

    try:
        return await asyncio.to_thread(_grant)
    except Exception:
        logger.warning(
            "Failed to grant prefix-scoped artifact write for compute job %s",
            job_id,
            exc_info=True,
        )
        return False


async def revoke_compute_job_artifact_writer(job_id, *, member: str) -> bool:
    """Remove the prefix-scoped write binding added for a compute job.

    Best-effort cleanup at explicit teardown; the grant also self-expires via its
    ``request.time`` condition when teardown never runs. Returns ``False`` on any
    error instead of raising. ``member`` is accepted for symmetry but the binding
    is located by its job-stable condition title.
    """

    title = _compute_job_artifact_condition_title(job_id)

    def _revoke() -> bool:
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        policy = bucket.get_iam_policy(requested_policy_version=3)
        policy.version = 3
        remaining = [
            binding
            for binding in policy.bindings
            if (binding.get("condition") or {}).get("title") != title
        ]
        if len(remaining) != len(policy.bindings):
            policy.bindings = remaining
            bucket.set_iam_policy(policy)
        return True

    try:
        return await asyncio.to_thread(_revoke)
    except Exception:
        logger.warning(
            "Failed to revoke prefix-scoped artifact write for compute job %s",
            job_id,
            exc_info=True,
        )
        return False


async def count_compute_job_artifacts(job_id) -> int:
    """Best-effort count of durable artifact objects under a job's prefix.

    Lists objects under ``<root>/<job_id>/artifacts/`` (the per-job durable
    output prefix the GCP startup script uploads to before poweroff) and returns
    how many exist. Used at finalize time to decide whether a terminal job
    actually produced durable artifacts worth linking as a first-class result.

    Best-effort by contract: returns ``0`` on ANY error (treated upstream as
    'no artifact') instead of raising — durable-artifact linkage must never
    block a job's terminal completion on a flaky GCS list call.
    """
    prefix = compute_job_artifact_object_prefix(job_id) + "/"

    def _count() -> int:
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        return sum(1 for _ in bucket.list_blobs(prefix=prefix))

    try:
        return await asyncio.to_thread(_count)
    except Exception:
        logger.warning(
            "Failed to count durable artifacts for compute job %s",
            job_id,
            exc_info=True,
        )
        return 0


async def upload_json_payload(task_id: str, payload_data: list | dict) -> str:
    """
    Uploads a JSON payload to GCS and returns the gs:// URI.
    This is used to pass large payloads (like images or complex instructions) to the VPM.
    """
    object_name = f"tasks/{task_id}/payload.json"
    logger.info(f"Uploading payload to gs://{ARTIFACT_BUCKET_NAME}/{object_name}")

    def _upload():
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        blob.upload_from_string(
            data=json.dumps(payload_data), content_type="application/json"
        )
        return f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    try:
        uri = await asyncio.to_thread(_upload)
        return uri
    except Exception:
        logger.exception("Failed to upload payload to GCS")
        raise


async def generate_upload_signed_url(
    task_id: str, filename: str, expiration_minutes: int = 15
) -> str:
    """
    Generates a v4 signed URL for uploading an object to GCS.
    This allows the agent VM to upload files directly to Cloud Storage
    without needing IAM permissions or routing heavy traffic through the API.
    """
    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    object_name = f"tasks/{task_id}/{safe_filename}"
    logger.info(
        f"Generating upload signed URL for gs://{ARTIFACT_BUCKET_NAME}/{object_name}"
    )

    def _generate():
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(minutes=expiration_minutes),
            method="PUT",
            content_type="application/octet-stream",  # Can be overridden by client
        )

    try:
        url = await asyncio.to_thread(_generate)
        return url
    except Exception:
        logger.exception("Failed to generate upload signed URL")
        raise
