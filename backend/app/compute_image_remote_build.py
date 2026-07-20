"""Isolated Cloud Build implementation for project compute images.

Production code in this module uses Google APIs directly.  It never invokes a
local docker daemon or the gcloud CLI.  The repository checkout is only read to
produce a bounded, credential-free staging archive.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Sequence

from app import compute_image_build
from app.compute_image_build import BuildResult

logger = logging.getLogger(__name__)

_REMOTE_BUILD_TIMEOUT_SECONDS = 3600
_REMOTE_QUEUE_TIMEOUT_SECONDS = 600
_REMOTE_POLL_INTERVAL_SECONDS = 15
_BUILD_MACHINE_TYPE = "E2_HIGHCPU_8"
_MAX_CONTEXT_FILES = 20_000
_MAX_CONTEXT_BYTES = 512 * 1024 * 1024
_TERMINAL_BUILD_STATUSES = {
    "SUCCESS", "FAILURE", "TIMEOUT", "CANCELLED", "INTERNAL_ERROR", "EXPIRED"
}


def _get_builder_sa() -> str:
    return os.environ.get("COMPUTE_IMAGE_BUILDER_SA", "").strip()


def _get_build_region() -> str:
    return os.environ.get("COMPUTE_IMAGE_BUILD_LOCATION", "").strip()


def _get_build_source_bucket() -> str:
    return os.environ.get("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "").strip()


def _get_gcp_project_id() -> str:
    project = os.environ.get("GCP_PROJECT_ID", "").strip()
    if not project:
        raise RuntimeError("GCP_PROJECT_ID must be set for remote compute-image builds")
    return project


def _normalize_builder_sa(sa_email: str, project_id: str) -> str:
    """Return the fully-qualified resource name Cloud Build requires for service_account.

    Cloud Build's Build.service_account must be
    ``projects/{PROJECT}/serviceAccounts/{EMAIL}``.  The env var is stored as a
    bare email address (e.g. ``image-builder-sa@proj.iam.gserviceaccount.com``);
    this normalises it so callers never need to know the difference.
    """
    if sa_email.startswith("projects/"):
        return sa_email
    return f"projects/{project_id}/serviceAccounts/{sa_email}"


def _remote_build_configured() -> bool:
    return bool(_get_builder_sa() and _get_build_region() and _get_build_source_bucket())


def _artifact_parent(full_repo: str) -> tuple[str, str]:
    """Return Artifact Registry repository resource and image path."""
    host, project, repository, *image = full_repo.split("/")
    if not image or not host.endswith("-docker.pkg.dev"):
        raise ValueError(f"not an Artifact Registry image repository: {full_repo!r}")
    location = host.removesuffix("-docker.pkg.dev")
    parent = f"projects/{project}/locations/{location}/repositories/{repository}"
    return parent, "/".join(image)


def _artifact_image_for_tag(full_repo: str, tag: str, *, _client=None):
    """Return the DockerImage carrying *tag*, failing open on registry errors."""
    try:
        from google.cloud import artifactregistry_v1

        parent, image_path = _artifact_parent(full_repo)
        client = _client or artifactregistry_v1.ArtifactRegistryClient()
        for image in client.list_docker_images(request={"parent": parent}):
            wanted = f"{full_repo}:{tag}"
            tags = list(getattr(image, "tags", ()) or ())
            resource_name = str(getattr(image, "name", ""))
            if (wanted in tags or tag in tags) and f"dockerImages/{image_path}@" in resource_name:
                return image
    except Exception as exc:  # noqa: BLE001 - registry miss is an optimization
        logger.warning("Artifact Registry tag lookup failed for %s:%s: %s", full_repo, tag, exc)
    return None


def remote_registry_tag_exists(full_repo: str, tag: str, *, _client=None, **_ignored) -> bool:
    return _artifact_image_for_tag(full_repo, tag, _client=_client) is not None


def _resolve_registry_digest_from_api(full_repo: str, tag: str, *, _client=None, **_ignored) -> str | None:
    image = _artifact_image_for_tag(full_repo, tag, _client=_client)
    if image is None:
        return None
    name = str(getattr(image, "name", ""))
    digest = name.rpartition("@")[2]
    return digest if digest.startswith("sha256:") else None


_ARCHIVE_SCANNER = r'''import sys, tarfile
import trusted_scanner as s
archive = sys.argv[1]
findings = []
with tarfile.open(archive, "r:*") as outer:
    layers = s._resolve_image_layers(outer)
    if not layers:
        raise RuntimeError("credential scan found no recognizable image layers")
    for label, member_name in layers:
        stream = outer.extractfile(outer.getmember(member_name))
        if stream is None:
            continue
        with stream, tarfile.open(fileobj=stream, mode="r:*") as layer:
            for member in layer:
                if not member.isfile() or not 0 < member.size <= s._MAX_SCANNED_FILE_BYTES:
                    continue
                fh = layer.extractfile(member)
                if fh is not None:
                    findings += s._scan_content_patterns(
                        f"layer:{label}!{member.name}", fh.read(s._MAX_SCANNED_FILE_BYTES)
                    )
    for label, entries in s._resolve_image_config_envs(outer):
        findings += s._scan_config_env_for_secrets(label, entries)
if findings:
    print("credential scan failed: " + "; ".join(findings), file=sys.stderr)
    raise SystemExit(42)
print("built image credential scan: clean")
'''


def _package_build_source(repo_dir: str, *, dockerfile: str, dep_files: Sequence[str],
                          build_args: Dict[str, str] | None = None) -> str:
    """Create a bounded archive, excluding git metadata and credential files."""
    del dockerfile, dep_files, build_args
    root = Path(repo_dir).resolve()
    tmpdir = tempfile.mkdtemp(prefix="compute-image-remote-src-")
    archive = os.path.join(tmpdir, "source.tar.gz")
    count = total = 0
    try:
        with tarfile.open(archive, "w:gz") as out:
            for path in root.rglob("*"):
                rel = path.relative_to(root)
                if ".git" in rel.parts or rel.parts[0] in {
                    ".cloudbuild", ".env", ".netrc", ".git-credentials",
                    ".npmrc", ".pypirc",
                }:
                    continue
                if path.is_symlink():
                    target = path.resolve()
                    if root not in target.parents and target != root:
                        raise ValueError(f"build context symlink escapes checkout: {rel}")
                if path.is_file():
                    count += 1
                    total += path.stat().st_size
                    if count > _MAX_CONTEXT_FILES or total > _MAX_CONTEXT_BYTES:
                        raise ValueError("build context exceeds file/byte limit")
                out.add(path, arcname=str(rel), recursive=False)

            scanner_source = Path(compute_image_build.__file__).read_bytes()
            for name, data, mode in (
                (".cloudbuild/trusted_scanner.py", scanner_source, 0o444),
                (".cloudbuild/scan_archive.py", _ARCHIVE_SCANNER.encode(), 0o555),
            ):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), mode, 0
                import io
                out.addfile(info, io.BytesIO(data))
        return archive
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


@dataclass
class _CloudBuildSubmission:
    build_id: str
    log_url: str
    staged_object: str


def _cloud_build_client(region: str):
    from google.cloud.devtools import cloudbuild_v1
    return cloudbuild_v1.CloudBuildClient(
        client_options={"api_endpoint": f"{region}-cloudbuild.googleapis.com"}
    )


def _submit_cloud_build(*, source_tarball: str, full_image_ref: str,
                        dockerfile_rel: str, build_args: Dict[str, str] | None,
                        parent: str | None, tag: str, region: str | None = None,
                        _client=None, _storage_client=None, **_ignored) -> _CloudBuildSubmission:
    """Upload the exact source object and submit it with the Cloud Build API."""
    del tag
    from google.cloud import storage
    from google.cloud.devtools import cloudbuild_v1

    region = region or _get_build_region()
    project = _get_gcp_project_id()
    object_name = f"build-sources/{uuid.uuid4().hex}.tar.gz"
    storage_client = _storage_client or storage.Client(project=project)
    blob = storage_client.bucket(_get_build_source_bucket()).blob(object_name)
    blob.upload_from_filename(source_tarball, timeout=300)

    args = ["build", "--network=none", "-f", dockerfile_rel, "-t", full_image_ref]
    merged_args = dict(build_args or {})
    if parent:
        merged_args.setdefault("PARENT_IMAGE", parent)
    for key, value in sorted(merged_args.items()):
        args += ["--build-arg", f"{key}={value}"]
    args.append(".")
    steps = [
        {"name": "gcr.io/cloud-builders/docker", "args": args},
        {"name": "gcr.io/cloud-builders/docker", "args": ["save", "-o", "/workspace/.cloudbuild/image.tar", full_image_ref]},
        {"name": "python:3.11-slim", "entrypoint": "python3", "args": ["/workspace/.cloudbuild/scan_archive.py", "/workspace/.cloudbuild/image.tar"]},
        {"name": "gcr.io/cloud-builders/docker", "args": ["push", full_image_ref]},
    ]
    build = cloudbuild_v1.Build(
        source=cloudbuild_v1.Source(storage_source=cloudbuild_v1.StorageSource(
            bucket=_get_build_source_bucket(), object_=object_name)),
        steps=steps, timeout={"seconds": _REMOTE_BUILD_TIMEOUT_SECONDS},
        queue_ttl={"seconds": _REMOTE_QUEUE_TIMEOUT_SECONDS},
        service_account=_normalize_builder_sa(_get_builder_sa(), project), images=[full_image_ref],
        options=cloudbuild_v1.BuildOptions(machine_type=_BUILD_MACHINE_TYPE,
                                           logging="CLOUD_LOGGING_ONLY"),
    )
    client = _client or _cloud_build_client(region)
    try:
        operation = client.create_build(request={"project_id": project, "build": build,
                                                 "parent": f"projects/{project}/locations/{region}"})
        metadata = operation.metadata
        response = getattr(metadata, "build", metadata)
        build_id = str(getattr(response, "id", ""))
        if not build_id:
            raise RuntimeError("Cloud Build submission returned no build id")
        return _CloudBuildSubmission(build_id, str(getattr(response, "log_url", "")), object_name)
    except BaseException:
        with contextlib.suppress(Exception):
            blob.delete()
        raise


def _cancel_cloud_build(build_id: str, *, region: str | None = None, _client=None, **_ignored) -> bool:
    region = region or _get_build_region()
    try:
        client = _client or _cloud_build_client(region)
        client.cancel_build(request={"project_id": _get_gcp_project_id(), "id": build_id,
                                     "name": f"projects/{_get_gcp_project_id()}/locations/{region}/builds/{build_id}"})
        return True
    except Exception:
        logger.exception("failed to cancel Cloud Build %s", build_id)
        return False


def _poll_cloud_build(build_id: str, *, region: str | None = None,
                      deadline_at: float | None = None, cancel_event=None,
                      _client=None, _sleep=time.sleep, **_ignored) -> dict:
    from app.compute_image_build import BuildAborted, current_build_control
    region = region or _get_build_region()
    client = _client or _cloud_build_client(region)
    while True:
        control = current_build_control()
        cancelled = (cancel_event is not None and cancel_event.is_set()) or (
            control is not None and control.aborted
        )
        if cancelled or (deadline_at is not None and time.monotonic() > deadline_at):
            _cancel_cloud_build(build_id, region=region, _client=client)
            if cancelled:
                raise BuildAborted(f"Cloud Build {build_id} cancelled by caller")
            raise TimeoutError(f"Cloud Build {build_id} exceeded its deadline and was cancelled")
        build = client.get_build(request={"project_id": _get_gcp_project_id(), "id": build_id,
                                          "name": f"projects/{_get_gcp_project_id()}/locations/{region}/builds/{build_id}"})
        status = getattr(build.status, "name", str(build.status))
        result = {"status": status, "failureInfo": {"message": getattr(build.failure_info, "detail", "")}}
        if status in _TERMINAL_BUILD_STATUSES:
            return result
        _sleep(_REMOTE_POLL_INTERVAL_SECONDS)


def _wait_for_cloud_build_terminal(build_id: str, *, region: str | None = None,
                                    _client=None, _sleep=time.sleep,
                                    timeout: float = 120.0) -> None:
    """Poll until the remote build enters a terminal state, then return.

    Called after ``_cancel_cloud_build`` to ensure the staged source object is
    not deleted (or the semaphore slot released) while the remote build is still
    executing.  Fails open on any API error so a polling failure never blocks
    staged-source cleanup.
    """
    region = region or _get_build_region()
    project = _get_gcp_project_id()
    try:
        client = _client or _cloud_build_client(region)
    except Exception:  # noqa: BLE001
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            build = client.get_build(
                request={"project_id": project, "id": build_id,
                         "name": f"projects/{project}/locations/{region}/builds/{build_id}"}
            )
            status = getattr(build.status, "name", str(build.status))
            if status in _TERMINAL_BUILD_STATUSES:
                return
        except Exception:  # noqa: BLE001 — fail open: if we can't poll, stop waiting
            return
        _sleep(_REMOTE_POLL_INTERVAL_SECONDS)


def _delete_staged_object(object_name: str, *, _storage_client=None) -> None:
    from google.cloud import storage
    client = _storage_client or storage.Client(project=_get_gcp_project_id())
    client.bucket(_get_build_source_bucket()).blob(object_name).delete()


def _check_dockerfile_network_directives(content: bytes) -> None:
    """Reject Dockerfiles that contain per-RUN network overrides.

    Cloud Build runs ``docker build --network=none`` to deny ADC access to
    builder processes.  Docker allows each individual ``RUN`` instruction to
    override the global ``--network`` flag via ``RUN --network=<type>``.  A
    malicious project Dockerfile could restore network access this way,
    reaching Cloud Build ADC and the shared builder credentials.

    We reject any ``RUN --network=`` (or ``RUN --network ``) directive whose
    value is not ``none``.  Unrecognised or blank values are rejected to be
    safe.  Comments and blank lines are ignored.  Multi-line continuations
    (backslash at end of line) are joined before checking.
    """
    import re

    # Join continuation lines.
    raw = content.decode("utf-8", errors="replace")
    joined = re.sub(r"\\\n", " ", raw)

    # Match RUN [options] where --network appears; handle both
    # ``--network=<value>`` and ``--network <value>`` forms.
    run_network_re = re.compile(
        r"^\s*RUN\s+.*?--network[=\s]+(\S+)", re.IGNORECASE | re.MULTILINE
    )
    for match in run_network_re.finditer(joined):
        value = match.group(1).strip().lower().rstrip(",")
        if value != "none":
            raise ValueError(
                f"Dockerfile contains a per-RUN network override "
                f"'--network={match.group(1)}' that bypasses the Cloud Build "
                f"network isolation guard (--network=none).  Remove all "
                f"per-instruction RUN --network directives from the Dockerfile."
            )


def remote_build_and_push(*, repo_dir: str, registry_repo: str, image_name: str,
                          dockerfile: str, dep_files: Sequence[str], parent: str | None = None,
                          build_args: Dict[str, str] | None = None, dry_run: bool = False,
                          cancel_event=None, _tag_exists_fn=None, _submit_fn=None,
                          _poll_fn=None, _resolve_digest_fn=None, **_ignored) -> BuildResult:
    """Hash, validate, remotely build, scan, publish, and return a digest pin."""
    from app.compute_image_build import CredentialLeakError
    if not _remote_build_configured():
        raise RuntimeError("Remote compute-image build is not configured")
    build_args = dict(build_args or {})
    violations = [k for k, value in build_args.items()
                  if k in compute_image_build._SECRET_ENV_NAMES and value]
    if violations:
        raise CredentialLeakError("build_args contains platform secret keys: " + ", ".join(sorted(violations)))

    dockerfile_path = os.path.join(repo_dir, dockerfile)
    if not os.path.isfile(dockerfile_path):
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")
    dockerfile_bytes = Path(dockerfile_path).read_bytes()
    _check_dockerfile_network_directives(dockerfile_bytes)
    dep_file_bytes: Dict[str, bytes] = {}
    for rel in dep_files:
        path = os.path.join(repo_dir, rel)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"declared dependency file not found: {path}")
        dep_file_bytes[rel] = Path(path).read_bytes()
    inputs = compute_image_build.assemble_fingerprint_inputs(
        dockerfile_bytes,
        dep_file_bytes,
        parent=parent,
        build_args=build_args,
    )
    dep_hash = compute_image_build.compute_dependency_hash(inputs)
    full_repo = f"{registry_repo}/{image_name}"
    tag_exists = _tag_exists_fn or remote_registry_tag_exists
    resolve_digest = _resolve_digest_fn or _resolve_registry_digest_from_api
    if tag_exists(full_repo, dep_hash):
        digest = resolve_digest(full_repo, dep_hash)
        return BuildResult(pinned_ref=f"{full_repo}@{digest}" if digest else f"{full_repo}:{dep_hash}",
                           tag=dep_hash, dep_hash=dep_hash, already_present=True, built=False, digest=digest)
    findings = compute_image_build.scan_inputs_for_credentials(inputs, dockerfile_names=["Dockerfile"])
    if findings:
        raise CredentialLeakError("credential scan failed before remote build: " + "; ".join(findings))
    if dry_run:
        return BuildResult(pinned_ref=f"{full_repo}:{dep_hash}", tag=dep_hash, dep_hash=dep_hash,
                           already_present=False, built=False, findings=[])

    archive = _package_build_source(repo_dir, dockerfile=dockerfile, dep_files=dep_files, build_args=build_args)
    submission = None
    try:
        submission = (_submit_fn or _submit_cloud_build)(
            source_tarball=archive, full_image_ref=f"{full_repo}:{dep_hash}",
            dockerfile_rel=dockerfile, build_args=build_args, parent=parent, tag=dep_hash)
        try:
            result = (_poll_fn or _poll_cloud_build)(
                submission.build_id,
                deadline_at=time.monotonic() + _REMOTE_BUILD_TIMEOUT_SECONDS + _REMOTE_QUEUE_TIMEOUT_SECONDS + 300,
                cancel_event=cancel_event)
        except BaseException:
            _cancel_cloud_build(submission.build_id)
            # Poll until the remote build reaches a terminal state before we
            # delete the staged source — prevents source removal while the
            # builder is still executing against it.
            _wait_for_cloud_build_terminal(submission.build_id)
            raise
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"Cloud Build {submission.build_id} failed: {result.get('status')}")
        digest = resolve_digest(full_repo, dep_hash)
        if not digest:
            raise RuntimeError("published image digest could not be resolved")
        return BuildResult(pinned_ref=f"{full_repo}@{digest}", tag=dep_hash, dep_hash=dep_hash,
                           already_present=False, built=True, digest=digest)
    finally:
        shutil.rmtree(os.path.dirname(archive), ignore_errors=True)
        if submission is not None:
            with contextlib.suppress(Exception):
                _delete_staged_object(submission.staged_object)


remote_build_and_push_cancellable = remote_build_and_push
