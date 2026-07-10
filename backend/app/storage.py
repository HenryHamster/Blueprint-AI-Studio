"""Google Cloud Storage helpers for agent task payloads and artifacts.

Wraps the blocking ``google.cloud.storage`` client in async helpers (run in a
thread) for uploading large task payloads to the artifact bucket as
``gs://`` URIs and for minting V4 signed upload URLs the agent VM can use to
push durable compute-job artifact bundles back. The bucket and GCP project are
read from the ``ARTIFACT_BUCKET_NAME`` / ``GCP_PROJECT_ID`` environment
variables.
"""

import os
import re
import uuid
import datetime
import logging
import asyncio
import json
import threading
import google.auth
from google.auth.transport.requests import Request as _GoogleAuthRequest
from google.cloud import storage

logger = logging.getLogger(__name__)

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project-id")
ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")

# Merged helpers from both sides:
# A gs:// URI is gs://<bucket>/<object>. Buckets allow lowercase letters,
# digits, dots, dashes and underscores; object names are far more permissive
# but we keep them to a safe printable subset (no control chars / spaces /
# backslashes) so a hand-supplied reference can't smuggle traversal or
# protocol-confusion tricks past the validator.
_GS_URI_RE = re.compile(r"^gs://([a-z0-9][a-z0-9._-]{1,221}[a-z0-9])/([A-Za-z0-9._/-]+)$")

# Object paths the agent writes for per-task artifacts/payloads all live under
# ``tasks/<task_uuid>/...``; pulling the UUID back out lets the resolver bind a
# storage reference to the owning task (and therefore project) before signing.
_TASK_OBJECT_RE = re.compile(
    r"^tasks/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/"
)

# Compute-job durable artifacts live under ``compute-jobs/<job_uuid>/...``.
_COMPUTE_JOB_OBJECT_RE = re.compile(
    r"^compute-jobs/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/"
)


class StorageUriError(ValueError):
    """A storage reference is malformed, uses a non-gs scheme, or points
    outside the configured artifact bucket. Callers should treat this as a
    client error (reject the reference) rather than a storage outage."""


def parse_artifact_uri(storage_uri: str) -> tuple[str, str]:
    """Parse and validate a ``gs://<bucket>/<object>`` artifact reference.

    Returns ``(bucket, object_name)``. Raises :class:`StorageUriError` when the
    reference is empty, not a string, not a well-formed ``gs://`` URI, or points
    at a bucket other than the configured :data:`ARTIFACT_BUCKET_NAME`. This is
    the single choke point for bucket-allowlisting — fail closed here and no
    out-of-bucket object can ever be signed.
    """
    if not isinstance(storage_uri, str) or not storage_uri:
        raise StorageUriError("storage reference is empty")
    match = _GS_URI_RE.match(storage_uri)
    if not match:
        raise StorageUriError(
            f"not a well-formed gs://<bucket>/<object> reference: {storage_uri!r}"
        )
    bucket, object_name = match.group(1), match.group(2)
    if bucket != ARTIFACT_BUCKET_NAME:
        raise StorageUriError(
            f"object is outside the artifact bucket (got bucket {bucket!r})"
        )
    # Defence in depth: ``..`` segments can't appear given the character class,
    # but reject explicitly so the intent is unmistakable.
    if ".." in object_name.split("/"):
        raise StorageUriError("object path contains a parent-directory segment")
    return bucket, object_name


def task_id_for_artifact_uri(storage_uri: str) -> uuid.UUID | None:
    """Return the owning task UUID encoded in a ``tasks/<uuid>/...`` artifact
    reference, or ``None`` if the object does not live under a task prefix.

    Used by the download resolver to bind a reference to a project before
    signing; a ``None`` result means the reference can't be authorized by task
    ownership and must be rejected by the caller.
    """
    try:
        _bucket, object_name = parse_artifact_uri(storage_uri)
    except StorageUriError:
        return None
    match = _TASK_OBJECT_RE.match(object_name)
    if not match:
        return None
    try:
        return uuid.UUID(match.group(1))
    except ValueError:
        return None


def compute_job_id_for_artifact_uri(storage_uri: str) -> uuid.UUID | None:
    """Return the owning compute-job UUID encoded in a ``compute-jobs/<uuid>/``
    artifact reference, or ``None`` if the object does not live under that prefix."""
    try:
        _bucket, object_name = parse_artifact_uri(storage_uri)
    except StorageUriError:
        return None
    match = _COMPUTE_JOB_OBJECT_RE.match(object_name)
    if not match:
        return None
    try:
        return uuid.UUID(match.group(1))
    except ValueError:
        return None


async def verify_artifact_project_ownership(
    storage_uri: str,
    project_id: uuid.UUID,
    session,
) -> None:
    """Assert that storage_uri belongs to project_id before any URL signing.

    Resolves the task UUID or compute-job UUID embedded in the URI path, fetches
    the corresponding DB row, and confirms its ``project_id`` matches the supplied
    ``project_id``. Raises :class:`StorageUriError` when the URI does not resolve
    to a recognised task or compute-job prefix (the caller cannot authorise it via
    ownership). Raises ``fastapi.HTTPException`` 404 when the resolved owner
    belongs to a different project — indistinguishable from "not found" so that
    cross-project UUID probing leaks nothing.

    Callers are still responsible for enforcing user membership in ``project_id``
    separately; this function only checks the artifact→project binding.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    task_uuid = task_id_for_artifact_uri(storage_uri)
    if task_uuid is not None:
        from app.models import Task  # noqa: PLC0415

        task = await session.get(Task, task_uuid)
        if task is None or task.project_id != project_id:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return

    job_uuid = compute_job_id_for_artifact_uri(storage_uri)
    if job_uuid is not None:
        from app.models import ComputeJob  # noqa: PLC0415

        job = await session.get(ComputeJob, job_uuid)
        if job is None or job.project_id != project_id:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return

    raise StorageUriError(
        "storage reference does not resolve to a task or compute-job prefix"
    )


# Cloud Run delivers the default service account as token-only
# ``google.auth.compute_engine.credentials.Credentials`` — an OAuth access token
# with NO private key — so google-cloud-storage's *local* V4 signer raises
# ``AttributeError: you need a private key to sign credentials``. The orchestrator
# SA is granted ``roles/iam.serviceAccountTokenCreator`` on itself
# (infra/terraform/iam.tf) precisely so signing can be delegated to the IAM
# Credentials ``signBlob`` API; supplying ``service_account_email`` +
# ``access_token`` to ``generate_signed_url`` selects that path. The ADC object is
# cached and refreshed under a lock because signing runs in a thread pool.
_iam_signer_lock = threading.Lock()
_iam_signer_credentials_cache = None


def _iam_signer_credentials():
    """Return refreshed ADC usable for IAM-API blob signing (SA email + token)."""
    global _iam_signer_credentials_cache
    with _iam_signer_lock:
        creds = _iam_signer_credentials_cache
        if creds is None:
            creds, _project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            _iam_signer_credentials_cache = creds
        if not creds.valid:
            creds.refresh(_GoogleAuthRequest())
        return creds


def _generate_v4_signed_url(blob, **kwargs) -> str:
    """Mint a V4 signed URL, delegating to IAM ``signBlob`` for token-only creds.

    Tries the library's local signer first so a real private key (developer ADC
    or unit tests) still signs without an IAM round-trip. On Cloud Run the local
    signer raises ``AttributeError`` because the metadata-server credentials hold
    no private key; we then sign via the IAM Credentials API by passing the
    service-account email + access token (requires
    ``roles/iam.serviceAccountTokenCreator`` on the runtime SA — already granted).
    """
    try:
        return blob.generate_signed_url(**kwargs)
    except AttributeError:
        creds = _iam_signer_credentials()
        return blob.generate_signed_url(
            **kwargs,
            service_account_email=creds.service_account_email,
            access_token=creds.token,
        )


async def generate_download_signed_url(
    storage_uri: str, expiration_minutes: int = 60
) -> str:
    """Validate ``storage_uri`` and mint a V4 signed GET URL for it.

    Bucket/scheme validation (:func:`parse_artifact_uri`) runs first and raises
    :class:`StorageUriError` for malformed, non-gs, or out-of-bucket references —
    no signing is attempted in that case, so a bad reference never leaks a URL.
    A failure of the underlying GCS client (credentials, network, missing
    object signing key) is re-raised as the original exception for the caller to
    map to a storage-unavailable response.

    **Caller responsibility — ownership check:** This function does not enforce
    task-to-project ownership; it only validates bucket membership and signs.
    Callers that accept a user-supplied ``gs://`` URI *must* verify that the
    referenced object belongs to the requesting project before calling:

    * Use :func:`task_id_for_artifact_uri` to extract the owning task UUID from a
      ``tasks/<uuid>/`` reference, then confirm ``task.project_id`` matches the
      caller's project.
    * Use :func:`compute_job_id_for_artifact_uri` for ``compute-jobs/<uuid>/``
      references, then confirm ``job.project_id`` matches.
    * Reject (404) any URI that cannot be bound to a project-owned record —
      fail closed rather than leaking whether the object exists.

    All production callers currently satisfy this contract; see
    ``GET /api/admin/artifacts/download-url`` in ``main.py`` for the reference
    implementation.
    """
    bucket_name, object_name = parse_artifact_uri(storage_uri)

    def _generate() -> str:
        blob = (
            storage.Client(project=GCP_PROJECT_ID).bucket(bucket_name).blob(object_name)
        )
        return _generate_v4_signed_url(
            blob,
            version="v4",
            expiration=datetime.timedelta(minutes=expiration_minutes),
            method="GET",
        )

    try:
        return await asyncio.to_thread(_generate)
    except Exception:
        logger.exception("Failed to generate download signed URL for %s", object_name)
        raise


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
        return _generate_v4_signed_url(
            blob,
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


async def get_compute_job_artifact_total_bytes(job_id) -> int:
    """Best-effort total byte size of durable artifact objects under a job's prefix.

    Same listing as :func:`count_compute_job_artifacts` but sums ``blob.size``
    instead of counting objects. Used together with the count to populate the
    ``durable_payload_bytes`` backend-derived metric for success-criterion evaluation.

    Best-effort by contract: returns ``0`` on ANY error — never raises.
    """
    prefix = compute_job_artifact_object_prefix(job_id) + "/"

    def _total() -> int:
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        return sum(b.size or 0 for b in bucket.list_blobs(prefix=prefix))

    try:
        return await asyncio.to_thread(_total)
    except Exception:
        logger.warning(
            "Failed to sum durable artifact bytes for compute job %s",
            job_id,
            exc_info=True,
        )
        return 0


async def download_compute_job_artifact_bundle(job_id) -> bytes | None:
    """Best-effort download of a compute job's durable artifact bundle bytes.

    Returns the raw ``.tar.gz`` bytes of the per-job bundle object
    (``<root>/<job_id>/artifacts/bundle.tar.gz``), or ``None`` when the object
    does not exist. Used by the GCP backend to recover a terminal job's durable
    ``terminal.json`` when the serial-console marker was lost to the
    poweroff-vs-serial-read race.

    Best-effort by contract: returns ``None`` on a missing object OR on ANY
    error (network, credentials, decode) instead of raising — the caller treats
    a ``None`` as 'no durable record' and falls back to its blind-FAILURE path,
    so reconciliation must never crash the compute-job monitor.
    """
    object_name = compute_job_artifact_object_name(job_id)

    def _download() -> bytes | None:
        storage_client = storage.Client(project=GCP_PROJECT_ID)
        bucket = storage_client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        if not blob.exists():
            return None
        return blob.download_as_bytes()

    try:
        return await asyncio.to_thread(_download)
    except Exception:
        logger.warning(
            "Failed to download durable artifact bundle for compute job %s",
            job_id,
            exc_info=True,
        )
        return None


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
