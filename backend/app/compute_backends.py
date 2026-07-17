"""Pluggable backends for external compute jobs.

Callers depend on the stable ``ComputeBackend`` interface only. Backend-specific
runtime details such as subprocess handling or GCP VM polling stay encapsulated
here, which lets future lanes add a second remote backend without changing the
compute-job monitor or API layer.

The persisted :class:`app.models.ComputeJob` row is the single source of truth.
Backends keep only best-effort in-memory handles (e.g. a local ``Popen``); every
field a backend needs to recover across a leader restart (reserved external id,
GCP zone, cost rate, launch command) lives on the row — see
``ComputeJob.polling_metadata`` and ``ComputeJob.external_compute_id``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Protocol, Sequence

from google.cloud import storage as gcs_storage

from app import compute
from app import storage
from app.compute_experiment_contract import ENV_KEY_RE
from app.models import ComputeJob, ComputeJobStatus, Project, ProjectComputeImage
from app.vm_lifecycle import _ensure_vm_deleted

# Domain-separation prefix for the compute-job heartbeat secret (see
# ``compute_job_heartbeat_secret``). Versioned so the derivation can be rotated
# without colliding with any other HMAC use of ``AGENT_JWT_SECRET``.
_COMPUTE_JOB_HB_SECRET_PREFIX = b"compute-job-heartbeat:v1:"


def compute_job_heartbeat_secret(job_id: object) -> str:
    """Derive the per-job bearer secret the compute VM sends on its heartbeat.

    The compute-job VM heartbeat endpoint used to authenticate with the bare job
    UUID as its bearer token. But the job UUID is a *resource identifier* — it
    appears in URL paths, agent poll responses, and serial-console/log output — so
    anyone who merely observes it could POST heartbeats and mutate watchdog-relevant
    state (feed the no-progress watchdog fake progress samples, or transition a
    stuck STARTING job to RUNNING to suppress the provisioning-deadline timeout).

    This returns a per-job secret that is NOT the UUID: an HMAC-SHA256 of the job
    id keyed by the orchestrator's ``AGENT_JWT_SECRET`` (the same signing secret
    the agent-JWT machinery already uses — no new key material to manage). The
    secret is derivable ONLY by the orchestrator; it is embedded into the VM's
    startup script (delivered as encrypted instance metadata) and never returned
    by any API, so knowing the job UUID does not let a caller compute it. The VM
    sends it as its heartbeat bearer; the endpoint recomputes and constant-time
    compares. See ``app.routers.agent_compute_jobs.compute_job_vm_heartbeat``.
    """
    secret = os.getenv("AGENT_JWT_SECRET")
    if not secret:
        # AGENT_JWT_SECRET is validated at auth-module import; if it is somehow
        # absent we must fail loudly rather than fall back to a guessable token.
        raise RuntimeError(
            "AGENT_JWT_SECRET is required to derive the compute-job heartbeat secret"
        )
    message = _COMPUTE_JOB_HB_SECRET_PREFIX + str(job_id).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


# Scripts baked into the GCP compute-job startup script as base64 blobs.
# Loaded at module import time so a missing file fails fast at startup, not
# during live VM provisioning. See backend/app/_compute_scripts/__init__.py.
_COMPUTE_SCRIPTS_DIR = Path(__file__).parent / "_compute_scripts"
_COMMAND_RUNNER_B64 = base64.b64encode(
    (_COMPUTE_SCRIPTS_DIR / "command_runner.py").read_bytes()
).decode("ascii")
_ARTIFACT_UPLOAD_B64 = base64.b64encode(
    (_COMPUTE_SCRIPTS_DIR / "artifact_upload.py").read_bytes()
).decode("ascii")
_TERMINAL_RECORD_B64 = base64.b64encode(
    (_COMPUTE_SCRIPTS_DIR / "terminal_record.py").read_bytes()
).decode("ascii")
_ARTIFACT_DOWNLOAD_B64 = base64.b64encode(
    (_COMPUTE_SCRIPTS_DIR / "artifact_download.py").read_bytes()
).decode("ascii")
_HEARTBEAT_BODY_B64 = base64.b64encode(
    (_COMPUTE_SCRIPTS_DIR / "heartbeat_body.py").read_bytes()
).decode("ascii")
_VIEWABLE_WATCH_B64 = base64.b64encode(
    (_COMPUTE_SCRIPTS_DIR / "viewable_watch.py").read_bytes()
).decode("ascii")

logger = logging.getLogger(__name__)


def _load_result_json(result_path: str) -> Any:
    """Synchronous helper for run_preflight_smoke — offloaded via asyncio.to_thread
    so that file I/O and json.loads don't block the event loop."""
    return json.loads(Path(result_path).read_text(encoding="utf-8"))


def _is_safe_tar_member(name: str) -> bool:
    """True when a tar member name is safe to extract under a destination dir.

    Rejects absolute paths and any member that would escape the destination via
    ``..`` traversal (either a leading component or an embedded one). Both POSIX
    (``/``) and Windows (``\\``) separators are normalized so a crafted bundle
    cannot slip a traversal segment past the check.
    """
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        return False
    # os.path.splitdrive guards against Windows drive-absolute (``C:\\``) members.
    if os.path.splitdrive(normalized)[0]:
        return False
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    return ".." not in parts


def _extract_safe_members(tar: tarfile.TarFile, dest_dir: str) -> int:
    """Extract every path-traversal-safe regular member of an OPEN archive.

    Shared core of the bytes- and path-based extractors below. Members with
    absolute paths or ``..`` traversal are SKIPPED (never extracted), so a
    malicious or corrupt predecessor bundle can only ever write inside
    ``dest_dir``. Symlink / hardlink members are skipped as well — their targets
    could point outside the destination even when the member name itself is safe.
    Returns the count of members actually extracted.
    """
    os.makedirs(dest_dir, exist_ok=True)
    extracted = 0
    for member in tar.getmembers():
        if member.islnk() or member.issym():
            continue
        if not _is_safe_tar_member(member.name):
            logger.warning(
                "Skipping unsafe artifact-bundle member %r during rehydration",
                member.name,
            )
            continue
        tar.extract(member, path=dest_dir, filter="data")
        extracted += 1
    return extracted


def _safe_extract_artifact_bundle(bundle: bytes, dest_dir: str) -> int:
    """Extract an IN-MEMORY ``.tar.gz`` artifact bundle into ``dest_dir``.

    Path-traversal-safe (see :func:`_extract_safe_members`). Prefer
    :func:`_safe_extract_artifact_bundle_path` for a durable results bundle whose
    size is unbounded — this variant materializes the whole archive as ``bytes``
    and is an OOM risk on a multi-GB checkpoint bundle.

    Raises ``tarfile.TarError`` (or ``OSError``) on an unreadable/corrupt archive
    so the caller can fail the job with a clear reason rather than proceeding
    against a half-populated workspace.
    """
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        return _extract_safe_members(tar, dest_dir)


def _safe_extract_artifact_bundle_path(path: str, dest_dir: str) -> int:
    """Extract an ON-DISK ``.tar.gz`` artifact bundle into ``dest_dir``.

    The memory-bounded counterpart of :func:`_safe_extract_artifact_bundle`: the
    archive is read from *path* (a seekable file) and each member is streamed to
    disk by ``tarfile``, so peak memory is a tar block — NOT the bundle size. This
    is what a multi-GB RL checkpoint bundle must go through so rehydration cannot
    OOM the leader. Same path-traversal / symlink safety and return contract, and
    raises the same ``tarfile.TarError`` / ``OSError`` on a corrupt archive.
    """
    with tarfile.open(path, mode="r:gz") as tar:
        return _extract_safe_members(tar, dest_dir)


async def _rehydrate_input_artifact_bundle(
    input_artifact_job_id: str, dest_dir: str
) -> int:
    """Fetch a predecessor's durable bundle and extract it into ``dest_dir``.

    The bundle is STREAMED to a temp file on disk (never materialized whole in
    the orchestrator heap) and extracted from there, so peak memory during a
    cross-job checkpoint resume is bounded by the download chunk size, not by the
    bundle size — a multi-GB RL checkpoint bundle cannot OOM the leader. This
    mirrors the viewable pass's streaming seam
    (:func:`_download_artifact_bundle_to_path`); the whole-object
    ``storage.download_compute_job_artifact_bundle`` stays reserved for the tiny
    ``terminal.json`` recovery read.

    Raises ``RuntimeError`` when the predecessor bundle is missing or unreadable
    so the job fails LOUDLY with a clear reason instead of silently running
    against an empty workspace. Returns the number of files rehydrated.
    """
    handle, tmp_path = tempfile.mkstemp(prefix="bp-resume-", suffix=".tar.gz")
    os.close(handle)
    staging_dir = f"{dest_dir}.resume-staging"
    await asyncio.to_thread(shutil.rmtree, staging_dir, True)
    await asyncio.to_thread(lambda: os.makedirs(staging_dir, exist_ok=True))
    try:
        try:
            downloaded = await asyncio.to_thread(
                _download_artifact_bundle_to_path, input_artifact_job_id, tmp_path
            )
        except Exception as exc:  # surface as a loud, typed failure (re-raised)
            raise RuntimeError(
                "input_artifact_rehydration_failed: predecessor bundle for "
                f"{input_artifact_job_id} could not be downloaded: {exc}"
            ) from exc
        if not downloaded:
            raise RuntimeError(
                "input_artifact_rehydration_failed: predecessor compute job "
                f"{input_artifact_job_id} has no durable artifact bundle to resume from"
            )
        try:
            file_count = await asyncio.to_thread(
                _safe_extract_artifact_bundle_path, tmp_path, staging_dir
            )
        except (tarfile.TarError, OSError) as exc:
            raise RuntimeError(
                "input_artifact_rehydration_failed: predecessor bundle for "
                f"{input_artifact_job_id} could not be extracted: {exc}"
            ) from exc
        if file_count <= 0:
            raise RuntimeError(
                "input_artifact_rehydration_failed: predecessor bundle for "
                f"{input_artifact_job_id} contained no checkpoint files"
            )
        try:
            await asyncio.to_thread(shutil.rmtree, dest_dir, True)
            await asyncio.to_thread(os.replace, staging_dir, dest_dir)
        except OSError as exc:
            raise RuntimeError(
                "input_artifact_rehydration_failed: extracted checkpoint could not "
                f"be published: {exc}"
            ) from exc
        return file_count
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass
        if await asyncio.to_thread(os.path.exists, staging_dir):
            await asyncio.to_thread(shutil.rmtree, staging_dir, True)

_SERIAL_STATUS_PREFIX = "BLUEPRINT_COMPUTE_STATUS="
_SERIAL_EXIT_PREFIX = "BLUEPRINT_COMPUTE_EXIT_CODE="
_SERIAL_REASON_PREFIX = "BLUEPRINT_COMPUTE_REASON="
# Single-line, base64-encoded marker carrying the command's own result metrics so
# no metric value has to survive serial/escape passes — see CLAUDE.md's rule on
# generating one language from another.
_SERIAL_METRICS_PREFIX = "BLUEPRINT_COMPUTE_METRICS="
# Single-line, base64-encoded marker carrying the command's bounded combined log.
_SERIAL_COMMAND_LOG_PREFIX = "BLUEPRINT_COMPUTE_COMMAND_LOG="
# Single-line, base64-encoded marker carrying the echoed release SHA so the
# completion handler can verify the runtime was bound to the promoted release.
_SERIAL_RELEASE_ECHO_PREFIX = "BLUEPRINT_COMPUTE_RELEASE_ECHO="
# Single-line, base64-encoded marker carrying the cross-job checkpoint resume
# provenance from the startup script (ok, file_count, reason). Emitted on both
# the success and the fail-closed branches of the resume block so the operator
# can always tell whether a resumed job continued from its predecessor's
# checkpoint or restarted from zero — the waste the direction exists to prevent.
_SERIAL_RESUME_PREFIX = "BLUEPRINT_COMPUTE_RESUME="
# Pre-computed base64-encoded resume-provenance payload for the fail-closed
# branch of the startup-script resume block (hydration never started). Used in
# ``_build_gcp_startup_script`` where the static string avoids on-VM base64
# encoding overhead for a known-at-generation-time value.
_SERIAL_RESUME_FAILED_B64: str = base64.b64encode(
    json.dumps(
        {"ok": False, "file_count": 0, "reason": "resume_hydration_failed"},
        separators=(",", ":"),
    ).encode()
).decode()

# Well-known env var pointing the launched command at a result file it may write
# (a small flat JSON object of metric-name -> number). Absence = no metrics.
RESULT_FILE_ENV = "BLUEPRINT_RESULT_FILE"
# Well-known env var pointing the launched command at a durable, writable
# directory for ALL non-metric outputs (checkpoints, plots, logs, large files).
# The narrow numeric RESULT_FILE channel keeps working unchanged alongside it;
# this directory is the channel for everything that doesn't fit there. Its
# contents survive process/VM teardown: the local backend keeps the directory on
# disk under the job's state root, and the GCP backend uploads it to a per-job
# durable object prefix before poweroff.
ARTIFACT_DIR_ENV = "BLUEPRINT_ARTIFACT_DIR"
# Well-known env var pointing the launched command at a progress file it may
# periodically touch or write to signal liveness. Any write (mtime bump or
# changed content) counts as progress. The no-progress watchdog polls this
# file; when no_progress_timeout_seconds is set and the file has not advanced
# within that window, the job is killed early with terminal reason
# ``no_progress_timeout`` and exit code 125.
PROGRESS_FILE_ENV = "BLUEPRINT_PROGRESS_FILE"
# Reserved exit code the no-progress watchdog writes when it kills a stalled job.
# This value is a CONTRACT with three distinct producers — the local-subprocess
# wrapper (``_build_local_wrapper_command`` below), the GCP startup script, and
# the self-hosted runner (``scripts/local_runner.py``) — and one consumer,
# ``RunnerSubprocessBackend.poll_status``, which maps it to terminal reason
# ``no_progress_timeout`` (→ ``NO_PROGRESS_TIMEOUT``). Naming the value so the
# emitter and the detector share a single source of truth: were the two literals
# to silently drift, the platform kill would collapse to ``CODE_ERROR`` — burning
# retry budget and misattributing blame to the operator's command.
NO_PROGRESS_WATCHDOG_EXIT_CODE = 125
# Designated result-file path the command writes; read back after it finishes.
_GCP_RESULT_FILE_PATH = "/tmp/blueprint_compute_result.json"
# On-VM artifact directory the GCP startup script provisions and uploads.
_GCP_ARTIFACT_DIR_PATH = "/tmp/blueprint_compute_artifacts"
_GCP_ARTIFACT_BUNDLE_PATH = "/tmp/blueprint_compute_artifacts.tar.gz"
_GCP_PROGRESS_FILE_PATH = "/tmp/blueprint_compute_progress.json"
# Durable terminal record the startup script writes into the artifact dir right
# before poweroff. It carries the authoritative outcome (status/exit/reason +
# the same base64 metric/log/release payloads as the serial markers) so a job
# whose serial marker is lost to the poweroff-vs-serial-read race can still be
# reconciled to its real SUCCEEDED/FAILED result from the durable GCS bundle.
_GCP_TERMINAL_RECORD_FILENAME = "terminal.json"
# Defensive ceiling on the durable terminal.json read out of the bundle — the
# record is small (metrics + a bounded log), so anything larger is treated as
# absent rather than parsed.
_MAX_TERMINAL_RECORD_DECODED_BYTES = 131072
# Caps for the individually-persisted viewable-artifact substrate: per-file byte
# ceiling and total object-count ceiling so a pathological run can't upload
# unbounded per-file objects. Files over the size cap stay inside the bundle.
_MAX_VIEWABLE_FILE_BYTES = 8 * 1024 * 1024  # 8 MiB per viewable file (in-memory)
_MAX_VIEWABLE_FILE_COUNT = 25  # at most N individually-addressable objects/job


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive-int env override, falling back to ``default`` on absent/garbled."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return value if value > 0 else default


# Videos (rollout/training footage — the artifact type the RL-witness direction
# names) routinely exceed the 8 MiB in-memory ceiling. They are STREAMED member
# by member to storage instead of buffered, so they get a much larger ceiling
# without ever entering the orchestrator's heap. Kept well under Postgres int4
# (~2.1 GiB) so ``file_size_bytes`` cannot overflow. Env-overridable.
_MAX_VIEWABLE_VIDEO_BYTES = _positive_int_env(
    "BLUEPRINT_MAX_VIEWABLE_VIDEO_BYTES", 512 * 1024 * 1024
)  # 512 MiB per streamed video


def _viewable_size_cap_for_kind(kind: str) -> int:
    """Per-file byte ceiling for a viewable candidate, by media kind.

    Videos are streamed (never buffered whole) so they get the larger
    ``_MAX_VIEWABLE_VIDEO_BYTES`` ceiling; images stay at the in-memory
    ``_MAX_VIEWABLE_FILE_BYTES`` cap. Reads the module globals dynamically so a
    test can monkeypatch either ceiling.
    """
    return _MAX_VIEWABLE_VIDEO_BYTES if kind == "video" else _MAX_VIEWABLE_FILE_BYTES
# Transfer chunk for STREAMING a durable artifact bundle to disk. A results
# bundle has no size bound (checkpoints, videos, logs), so it is never pulled
# into the orchestrator's heap: peak memory during the viewable pass is bounded
# by this chunk, not by the bundle.
_BUNDLE_DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MiB
# Hard ceiling on the artifact upload so an oversized tree can never wedge
# poweroff — the upload is abandoned (best-effort) once it elapses.
_GCP_ARTIFACT_UPLOAD_TIMEOUT_SECONDS = 180
_LOCAL_RESULT_FILENAME = "result.json"
_LOCAL_ARTIFACT_DIRNAME = "artifacts"
# Defensive bounds on the reported-metrics payload — a garbled/oversized marker
# must be tolerated as simply ABSENT, never raise.
_MAX_RESULT_METRICS = 64
_MAX_RESULT_METRICS_DECODED_BYTES = 8192
# Defensive bounds on the command-log payload. Noisy output should be truncated
# rather than allowed to balloon the durable result or wedge teardown.
_MAX_COMMAND_LOG_BYTES = 32768
_MAX_COMMAND_LOG_DECODED_BYTES = 65536

# How often the reaper's progress heartbeat fires during a compute-image build,
# so the lifecycle watchdog (REAPER_HANG_SECONDS=300) does not cancel the reaper
# while a multi-minute baked-image build is in progress. 60s is well under the
# 300s stall threshold, giving 4 missed beats before the watchdog acts.
_HEARTBEAT_INTERVAL = 60

class ComputeLaunchSpecError(ValueError):
    """A launch spec is malformed in a way no retry can fix.

    Raised only from code paths that run BEFORE any billable resource is
    provisioned, so the caller can terminalize the job immediately (FAILED, with
    this exception's message as the operator-visible terminal_reason) instead of
    re-attempting the identical launch every monitor tick until the provisioning
    deadline. Generic launch exceptions stay retryable — this class is the
    explicit opt-out for deterministic, zero-cost, spec-level rejections.
    """


LOCAL_BACKEND_NAME = "local-subprocess"
GCP_BACKEND_NAME = "gcp"
LOCAL_GPU_BACKEND_NAME = "local-gpu"
RUNNER_SUBPROCESS_BACKEND_NAME = "runner-subprocess"

# Nominal per-second cost attributed to operator-hardware (self-hosted runner)
# compute jobs. Blueprint does not pay cloud rates for a runner's box, but
# reporting EXACTLY $0 is itself a lie: it zeroes the cost/task denominator
# (making GPU-job spend invisible in efficiency analytics) and turns the runtime
# spend_cap into a no-op (nothing ever accrues toward the cap, so a runaway job
# is bounded only by the wall-clock deadline). A small non-zero amortized rate
# keeps cost/task honest and lets the spend_cap bound runtime. Default
# ~$0.36/hr (electricity + wear approximation); override per operator via env.
OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND = float(
    os.getenv("COMPUTE_OPERATOR_HARDWARE_RATE_PER_SECOND", "0.0001")
)


def is_zero_cost_backend(name: str) -> bool:
    """Return True for backends that run on operator/local hardware Blueprint does not pay for."""
    return name in (LOCAL_BACKEND_NAME, LOCAL_GPU_BACKEND_NAME, RUNNER_SUBPROCESS_BACKEND_NAME)


# Terminal statuses for a compute job — only completed jobs have a well-defined
# elapsed window (started_at -> completed_at) to price a cloud equivalent over.
_TERMINAL_COMPUTE_STATUSES = (
    ComputeJobStatus.SUCCEEDED,
    ComputeJobStatus.FAILED,
    ComputeJobStatus.CANCELLED,
    ComputeJobStatus.TIMED_OUT,
)


def cloud_equivalent_cost_usd(job: ComputeJob) -> float | None:
    """Dollars the self-hosted fleet *avoided* by running ``job`` locally.

    This is the on-demand GCP price the same job's machine/accelerator shape would
    have accrued over its actual run window (``started_at`` -> ``completed_at``),
    had it run on paid cloud compute instead of an operator-owned runner.

    Returns ``None`` (job does not contribute to the avoided rollup) when:
      * the backend is NOT a zero-cost/self-hosted backend (a cloud job avoided
        nothing — Blueprint actually paid for it),
      * the job is not terminal (no settled run window to price),
      * ``started_at`` or ``completed_at`` is missing, or the window is negative,
      * the machine/accelerator shape is unknown or unsupported by the rate table.

    Never raises: pricing an unpriceable shape yields ``None``, not an exception,
    so a single malformed row cannot 500 the fleet-stats endpoint.
    """
    # Import here to avoid a module-load cycle (vm_cost_rates is otherwise a leaf).
    from app.vm_cost_rates import AcceleratorRejection, get_compute_rate_per_second

    backend_name = getattr(job, "backend", None)
    if not backend_name or not is_zero_cost_backend(backend_name):
        return None
    if getattr(job, "status", None) not in _TERMINAL_COMPUTE_STATUSES:
        return None

    started_at = getattr(job, "started_at", None)
    completed_at = getattr(job, "completed_at", None)
    if started_at is None or completed_at is None:
        return None
    elapsed_seconds = (completed_at - started_at).total_seconds()
    if elapsed_seconds <= 0:
        return None

    accelerator = getattr(job, "accelerator_type", None)
    count = getattr(job, "accelerator_count", None) or 1
    try:
        rate_per_second = get_compute_rate_per_second(
            machine_type=getattr(job, "machine_type", None),
            accelerator=accelerator,
            count=count,
            spot=False,
        )
    except AcceleratorRejection:
        return None
    except (KeyError, ValueError):
        # Unknown machine type / unsupported shape — do not attribute a cost.
        return None

    if rate_per_second <= 0:
        return None
    return rate_per_second * elapsed_seconds

# Fixed in-container path where the job's state directory is mounted.
# The command writes result.json and artifacts/ here; the host-side mount makes
# them visible at the job's state dir path without any post-run copy step.
_CONTAINER_OUT_DIR = "/tmp/blueprint_out"

_DETECT_LOCAL_GPU_UNSET = object()
_detect_local_gpu_cache: object = _DETECT_LOCAL_GPU_UNSET


def detect_local_gpu() -> dict | None:
    """Detect a local GPU via nvidia-smi. Cached for process lifetime. Never raises."""
    global _detect_local_gpu_cache
    if _detect_local_gpu_cache is not _DETECT_LOCAL_GPU_UNSET:
        return _detect_local_gpu_cache  # type: ignore[return-value]
    result: dict | None = None
    try:
        if shutil.which("nvidia-smi") is not None:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                line = proc.stdout.strip().split("\n")[0]
                parts = [p.strip() for p in line.split(",", 1)]
                if len(parts) == 2:
                    name, mem_str = parts
                    result = {"name": name, "memory_mib": int(mem_str)}
    except Exception:
        pass
    _detect_local_gpu_cache = result
    return result


_LOCAL_STATE_ROOT = os.path.join(tempfile.gettempdir(), "blueprint-compute-jobs")
_LOCAL_PID_FILENAME = "pid"
_LOCAL_STARTED_AT_FILENAME = "started_at"
_LOCAL_TERMINAL_FILENAME = "terminal"


@dataclass(slots=True)
class ComputeLaunchSpec:
    job_id: uuid.UUID
    command: str
    machine_type: str
    spend_cap_usd: float
    deadline_at: datetime | None
    # Provisioning timeout used by the monitor to bound the launch phase. This
    # remains separate from the command's runtime budget so launch delay never
    # steals runtime from the command itself.
    provisioning_deadline_at: datetime | None = None
    # Runtime budget in seconds, counted from actual launch/start time. The
    # backend startup script and runtime watchdog both use this instead of the
    # prelaunch wall-clock deadline.
    runtime_deadline_seconds: int | None = None
    # Deterministically reserved external id, persisted on the row BEFORE the
    # external launch side-effect so a re-launch after a crash is idempotent.
    external_id: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    provisioning_model: str = "STANDARD"
    # Single-GPU accelerator (canonical short name, e.g. "T4"); None for CPU jobs.
    accelerator: str | None = None
    accelerator_count: int = 1
    # Optional repo checkout. When both are set, the startup script clones this
    # repository at ``repo_clone["ref"]`` into a scratch dir, cds there, and puts
    # the repo root on PYTHONPATH BEFORE the command runs — so the command can
    # ``import`` the project's packages (``from pkg ...`` / ``python3 -m pkg``).
    # ``repo_token`` is a short-lived installation token minted at LAUNCH time and
    # is never persisted on the job row. Absent ⇒ no checkout (legacy behavior).
    repo_clone: dict[str, str] | None = None
    repo_token: str | None = None
    # Optional setup step executed as `bash -lc <setup_step>` AFTER the repo clone
    # and BEFORE the main command. A non-zero exit fails the job immediately with
    # reason `setup_step_failed`; the setup output is captured to setup_step.log in
    # the artifact dir for diagnosis. Absent ⇒ no setup step (legacy behavior).
    setup_step: str | None = None
    # Optional container image URI (e.g. "nvcr.io/nvidia/tensorflow:25.01-tf2-py3").
    # When set, the execution backend wraps the command in `docker run` with the
    # result-file directory volume-mounted so output lands on the host unchanged.
    # Absent ⇒ existing bare bash -lc path (byte-for-byte unchanged).
    container_image: str | None = None
    # Registry credentials for private/gated registries.  Only {server, username}
    # survive to the persisted launch spec; password is never written to the DB.
    container_registry_auth: dict | None = None
    # Optional no-progress watchdog timeout in seconds. When set, the execution
    # backend monitors the progress file (BLUEPRINT_PROGRESS_FILE) and terminates
    # the job early with exit code 125 + terminal reason ``no_progress_timeout``
    # if the file's mtime/size has not advanced within this window. Unset/None
    # means the watchdog is inert and the job runs to its full runtime deadline.
    no_progress_timeout_seconds: int | None = None
    # Optional cross-job checkpoint resume source: the id of a prior compute job
    # whose durable artifact bundle is fetched and extracted into the artifact dir
    # BEFORE the command runs, so a long job resumes from its predecessor's latest
    # checkpoint instead of restarting from zero. Validated at launch time (same
    # project, durable bundle present). Absent ⇒ no resume (existing behavior).
    input_artifact_job_id: str | None = None


@dataclass(slots=True)
class ComputeLaunchResult:
    external_id: str
    started_at: datetime
    cost_rate_per_second: float
    message: str | None = None
    # Extra fields merged into the job's polling_metadata (e.g. GCP zone, pid).
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pre-flight smoke
# ---------------------------------------------------------------------------

PREFLIGHT_SMOKE_TIMEOUT_SECONDS = int(
    os.getenv("COMPUTE_PREFLIGHT_SMOKE_TIMEOUT_SECONDS", "30")
)
_PREFLIGHT_STDERR_MAX_BYTES = 2000


@dataclass(slots=True)
class PreflightSmokeResult:
    startup_ok: bool
    exit_code: int | None  # None = timed out (still running = startup OK)
    metric_captured: bool
    declared_metric_value: float | None
    error_excerpt: str
    # True when a container_image was declared but Docker was NOT available on the
    # orchestrator host, so the smoke degraded to a bare-host ``bash -lc`` run
    # instead of the substrate-faithful containerized path.  Visible to the caller
    # and diagnostics so a degraded smoke is never mistaken for a containerized one.
    degraded_no_docker: bool = False
    # True when the smoke actually wrapped the command in ``docker pull`` +
    # ``docker run`` (container_image declared AND Docker present).
    ran_containerized: bool = False


async def run_preflight_smoke(
    command: str,
    *,
    declared_metric: str | None = None,
    timeout_seconds: int = PREFLIGHT_SMOKE_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    setup_step: str | None = None,
    container_image: str | None = None,
    container_registry_auth: dict | None = None,
) -> PreflightSmokeResult:
    """Run a cheap local pre-flight smoke to catch commands that die on a DB-less substrate.

    Runs `command` (and optionally `setup_step`) locally with no DB credentials
    injected.  A non-zero exit before the timeout is a FAIL; a zero exit or a
    timeout (command still alive) is a PASS (plus optional metric check).

    When ``container_image`` is set AND Docker is available on the orchestrator
    host (``shutil.which("docker")``), the *main* command is wrapped in the SAME
    ``docker login`` (optional) → ``docker pull`` → ``docker run`` path the real
    local-subprocess backend uses (see ``_build_local_wrapper_command``), so the
    smoke is substrate-faithful: it catches image-pull failures, in-container
    import errors, and missing CUDA/GPU libraries before a full VM is billed.
    The smoke's ``tmpdir`` is bind-mounted as the container out dir so the
    ``result.json`` written *inside* the container lands at the host path this
    function already reads, and ``RESULT_FILE_ENV`` is set inside the container by
    the run prefix (not via ``run_env``).  ``gpus`` is enabled when a local GPU is
    detected, matching the real local path.

    The ``setup_step`` (if any) always runs OUTSIDE the container as a bare
    ``bash -lc`` — consistent with the real local-subprocess backend, which runs
    ``setup_step_block`` before the ``docker run`` line (dependency install happens
    on the host/VM, not in the experiment image).

    GRACEFUL DEGRADATION: when ``container_image`` is set but Docker is NOT
    available on the orchestrator host, the smoke does NOT hard-fail — it falls
    back to the existing bare-host ``bash -lc`` path (so the gate still catches
    bare import/crash failures) and flags ``degraded_no_docker=True`` on the
    result so the caller and diagnostics can see the smoke was not substrate-
    faithful.  In that degraded mode the gate is NOT image-pull/in-container/CUDA
    faithful.  The absent-``container_image`` path is byte-for-byte unchanged.
    """
    run_env: dict[str, str] = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    # Mirror the GCP startup script: put the repo root on PYTHONPATH so commands
    # that import project packages work the same way they do on the compute VM.
    # Path(__file__) is backend/app/compute_backends.py; three levels up is the
    # repo root (the equivalent of /tmp/blueprint_repo on the GCP VM).
    _repo_root = str(Path(__file__).parent.parent.parent)
    _existing_pp = os.environ.get("PYTHONPATH", "")
    run_env["PYTHONPATH"] = f"{_repo_root}:{_existing_pp}" if _existing_pp else _repo_root
    run_env.update(env or {})

    tmpdir = tempfile.mkdtemp(prefix="bp_smoke_")
    try:
        result_path = os.path.join(tmpdir, "result.json")
        run_env[RESULT_FILE_ENV] = result_path

        # When a container image is declared, wrap the MAIN command in the same
        # docker login/pull/run path the real local backend uses — but only if
        # Docker is actually available on this orchestrator host.  Otherwise
        # degrade gracefully to the bare-host smoke (current behavior) rather than
        # hard-failing the job for a missing orchestrator-side dependency.
        degraded_no_docker = False
        ran_containerized = False
        effective_command = command
        if container_image:
            if shutil.which("docker") is not None:
                ran_containerized = True
                prefix = _build_container_run_prefix(
                    container_image,
                    host_out_dir=tmpdir,  # -> result.json lands at result_path
                    gpus=await asyncio.to_thread(detect_local_gpu) is not None,
                )
                docker_parts: list[str] = []
                auth = container_registry_auth or {}
                pwd = str(auth.get("password") or "")
                # The persisted auth carries only server/username (the password is
                # stripped at create time), so for orchestrated jobs this login
                # branch is dormant and password-gated pulls may fail at smoke —
                # acceptable: public images and host-identity pulls still validate.
                if pwd:
                    server_q = shlex.quote(str(auth.get("server") or ""))
                    user_q = shlex.quote(str(auth.get("username") or ""))
                    docker_parts.append(
                        f"printf '%s' {shlex.quote(pwd)} | "
                        f"docker login {server_q} -u {user_q} --password-stdin "
                        ">/dev/null 2>&1 &&"
                    )
                docker_parts.append(f"docker pull {shlex.quote(container_image)} &&")
                docker_parts.append(f"{prefix} bash -lc {shlex.quote(command)}")
                effective_command = " ".join(docker_parts)
            else:
                # Docker absent on the orchestrator: degrade, do not hard-fail.
                degraded_no_docker = True

        async def _run_bash(cmd: str, t: int) -> tuple[int | None, str]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-lc", cmd,
                    env=run_env,
                    cwd=cwd or None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, raw_err = await asyncio.wait_for(proc.communicate(), timeout=t)
                    excerpt = raw_err[-_PREFLIGHT_STDERR_MAX_BYTES:].decode("utf-8", "replace")
                    return proc.returncode, excerpt
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                    return None, ""
            except Exception as exc:  # pragma: no cover
                return -1, str(exc)[:_PREFLIGHT_STDERR_MAX_BYTES]

        # Run setup_step first if present; a non-zero (non-timeout) exit is a FAIL.
        # setup_step always runs OUTSIDE the container (bare bash -lc), consistent
        # with the real local-subprocess backend which runs setup before docker run.
        if setup_step:
            setup_t = max(10, timeout_seconds // 3)
            setup_exit, setup_err = await _run_bash(setup_step, setup_t)
            if setup_exit is not None and setup_exit != 0:
                return PreflightSmokeResult(
                    startup_ok=False,
                    exit_code=setup_exit,
                    metric_captured=False,
                    declared_metric_value=None,
                    error_excerpt=f"setup_step failed (exit {setup_exit}): {setup_err}",
                    degraded_no_docker=degraded_no_docker,
                    ran_containerized=ran_containerized,
                )

        exit_code, stderr_text = await _run_bash(effective_command, timeout_seconds)
        # Timed out (still running) counts as startup OK.
        startup_ok = exit_code is None or exit_code == 0

        # Check declared metric in result file when the command exited cleanly.
        metric_captured = declared_metric is None  # no metric required = always captured
        declared_metric_value: float | None = None
        if declared_metric and os.path.exists(result_path):
            try:
                parsed = await asyncio.to_thread(
                    _load_result_json, result_path
                )
                if isinstance(parsed, dict):
                    val = parsed.get(declared_metric)
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        metric_captured = True
                        declared_metric_value = float(val)
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        return PreflightSmokeResult(
            startup_ok=startup_ok,
            exit_code=exit_code,
            metric_captured=metric_captured,
            declared_metric_value=declared_metric_value,
            error_excerpt=stderr_text,
            degraded_no_docker=degraded_no_docker,
            ran_containerized=ran_containerized,
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, True)


@dataclass(slots=True)
class ComputeStatusResult:
    status: ComputeJobStatus
    message: str | None = None
    exit_code: int | None = None
    finished_at: datetime | None = None
    # Result metrics the launched command reported about itself (accuracy,
    # tokens_per_sec, final_loss, ...). None means the command reported nothing
    # OR the marker was missing/garbled — both treated identically as ABSENT.
    metrics: dict[str, float] | None = None
    # Bounded combined stdout/stderr from the launched command.
    command_log: dict[str, Any] | None = None
    # Promoted release SHA echoed back by the startup script; None if absent or
    # garbled. Only populated for live-proof jobs where BLUEPRINT_RELEASE_SHA was
    # injected into the runtime environment.
    echoed_release_sha: str | None = None
    # Live output tail written by the runner during execution. Only populated for
    # RUNNING local jobs that have flushed a live_tail.b64 to the artifact dir.
    live_output: dict[str, Any] | None = None
    # Resume provenance from cross-job checkpoint hydration, decoded from the
    # ``_SERIAL_RESUME_PREFIX`` serial marker (GCP backend) or stamped at launch
    # time (local backend). ``None`` means no resume was attempted.
    resume_provenance: dict[str, Any] | None = None


@dataclass(slots=True)
class ComputeCostResult:
    total_cost_usd: float
    measured_at: datetime


@dataclass(slots=True)
class TerminalArtifactReference:
    """Durable location of a terminal compute job's non-metric artifacts.

    ``uri`` is the canonical durable reference (``file://`` for the local
    backend, ``gs://`` for GCP), ``backend`` is the producing backend's name,
    and ``file_count`` is the number of durable objects detected under the
    location. A reference is only ever produced for a NON-empty location, so
    ``file_count`` is always >= 1 — an empty/absent location yields ``None``,
    never a zero-count reference.
    """

    uri: str
    backend: str
    file_count: int
    total_size_bytes: int = 0


@dataclass(slots=True)
class _LocalProcessHandle:
    proc: subprocess.Popen
    started_at: datetime


class ComputeBackend(Protocol):
    name: str

    def external_id_for(self, job: ComputeJob) -> str: ...

    async def launch(self, spec: ComputeLaunchSpec) -> ComputeLaunchResult: ...

    async def poll_status(self, job: ComputeJob) -> ComputeStatusResult: ...

    async def poll_cost(
        self, job: ComputeJob, *, as_of: datetime
    ) -> ComputeCostResult: ...

    async def terminate(self, job: ComputeJob, *, reason: str) -> bool: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LocalSubprocessComputeBackend:
    name = LOCAL_BACKEND_NAME

    def __init__(self) -> None:
        self._processes: dict[str, _LocalProcessHandle] = {}

    def external_id_for(self, job: ComputeJob) -> str:
        return f"local-{job.id}"

    async def launch(self, spec: ComputeLaunchSpec) -> ComputeLaunchResult:
        external_id = spec.external_id or f"local-{spec.job_id}"
        artifact_meta = _local_artifact_metadata(external_id)
        existing = self._processes.get(external_id)
        if existing is not None and existing.proc.poll() is None:
            # Idempotent re-launch: the reserved id is already running.
            return ComputeLaunchResult(
                external_id=external_id,
                started_at=existing.started_at,
                cost_rate_per_second=0.0,
                message=f"local subprocess already running pid={existing.proc.pid}",
                metadata=dict(artifact_meta),
            )
        persisted_pid = await asyncio.to_thread(
            _local_subprocess_pid_for_external_id, external_id
        )
        if persisted_pid is not None and await _aio_is_pid_running(persisted_pid):
            return ComputeLaunchResult(
                external_id=external_id,
                started_at=(
                    await asyncio.to_thread(
                        _local_subprocess_started_at_for_external_id, external_id
                    )
                )
                or _utcnow(),
                cost_rate_per_second=0.0,
                message=f"adopted existing local subprocess pid={persisted_pid}",
                metadata={"pid": persisted_pid, **artifact_meta},
            )
        if await asyncio.to_thread(
            _read_local_terminal_status_for_external_id, external_id
        ) is not None:
            metadata: dict[str, Any] = dict(artifact_meta)
            if persisted_pid is not None:
                metadata["pid"] = persisted_pid
            return ComputeLaunchResult(
                external_id=external_id,
                started_at=(
                    await asyncio.to_thread(
                        _local_subprocess_started_at_for_external_id, external_id
                    )
                )
                or _utcnow(),
                cost_rate_per_second=0.0,
                message="adopted completed local subprocess via terminal marker",
                metadata=metadata,
            )
        started_at = _utcnow()
        started_at_iso = started_at.replace(tzinfo=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        # Offload sync file I/O (os.makedirs, _write_local_state_text, os.remove)
        # to a thread-pool thread so the event loop is not blocked during job launch.
        await asyncio.to_thread(_launch_setup_state_dir, external_id, started_at_iso)
        # Cross-job checkpoint resume: rehydrate the predecessor's durable bundle
        # into the artifact dir BEFORE the wrapper command runs, so the command
        # sees the checkpoint already present in $BLUEPRINT_ARTIFACT_DIR. The
        # wrapper's later `mkdir -p` is idempotent and never removes these files.
        # A missing/unreadable bundle raises (fails the job LOUDLY) rather than
        # letting the command run against an empty workspace.
        # Stamp resume provenance into launch metadata so it flows through
        # polling_metadata and reaches the operator/agent surface via
        # serialize_compute_job_response.
        resume_provenance: dict[str, Any] | None = None
        if spec.input_artifact_job_id is not None:
            file_count = await _rehydrate_input_artifact_bundle(
                spec.input_artifact_job_id,
                _local_artifact_dir_for_external_id(external_id),
            )
            resume_provenance = {
                "ok": True,
                "file_count": file_count,
                "recorded_at": _utcnow().isoformat(),
                "resumed_from_job_id": str(spec.input_artifact_job_id),
            }
        env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
        env.update(spec.env or {})
        wrapper_command = _build_local_wrapper_command(
            spec.command,
            external_id=external_id,
            setup_step=spec.setup_step,
            container_image=spec.container_image,
            container_registry_auth=spec.container_registry_auth,
            cwd=spec.cwd,
            env=spec.env,
            repo_clone=spec.repo_clone,
            no_progress_timeout_seconds=spec.no_progress_timeout_seconds,
        )
        # Offload subprocess.Popen to a thread-pool thread — Popen itself is fast
        # but its preexec_fn / env setup can block the event loop.
        _cmd = ["bash", "-lc", wrapper_command]
        proc = await asyncio.to_thread(
            _launch_spawn_subprocess,
            _cmd,
            spec.cwd or None,
            env
        )
        self._processes[external_id] = _LocalProcessHandle(
            proc=proc, started_at=started_at
        )
        launch_metadata: dict[str, Any] = {"pid": proc.pid, **artifact_meta}
        if resume_provenance:
            launch_metadata["resume_provenance"] = resume_provenance
        return ComputeLaunchResult(
            external_id=external_id,
            started_at=started_at,
            cost_rate_per_second=0.0,
            message=f"spawned local subprocess pid={proc.pid}",
            metadata=launch_metadata,
        )

    async def poll_status(self, job: ComputeJob) -> ComputeStatusResult:
        handle = self._processes.get(job.external_compute_id or "")
        if handle is None:
            # Offload sync file I/O to a thread-pool thread
            terminal = await asyncio.to_thread(_read_local_terminal_status, job)
            if terminal is not None:
                return terminal
            pid = await asyncio.to_thread(_local_subprocess_pid, job)
            if pid is None:
                return ComputeStatusResult(
                    status=ComputeJobStatus.FAILED,
                    message="local subprocess no longer tracked and no pid was persisted",
                    finished_at=_utcnow(),
                )
            if await asyncio.to_thread(_is_pid_running, pid):
                live_output = await asyncio.to_thread(_read_local_live_tail, job)
                return ComputeStatusResult(
                    status=ComputeJobStatus.RUNNING,
                    live_output=live_output
                )
            terminal = await asyncio.to_thread(_read_local_terminal_status, job)
            if terminal is not None:
                return terminal
            return ComputeStatusResult(
                status=ComputeJobStatus.FAILED,
                message=f"local subprocess pid={pid} is no longer running",
                finished_at=_utcnow(),
            )
        return_code = handle.proc.poll()
        if return_code is None:
            live_output = await asyncio.to_thread(_read_local_live_tail, job)
            return ComputeStatusResult(
                status=ComputeJobStatus.RUNNING,
                live_output=live_output
            )
        self._processes.pop(job.external_compute_id or "", None)
        terminal = await asyncio.to_thread(_read_local_terminal_status, job)
        if terminal is not None:
            return terminal
        if return_code == 0:
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="local subprocess exited 0",
            )
        return ComputeStatusResult(
            status=ComputeJobStatus.FAILED,
            message=f"local subprocess exited with code {return_code}",
            exit_code=return_code,
            finished_at=_utcnow(),
        )

    async def poll_cost(self, job: ComputeJob, *, as_of: datetime) -> ComputeCostResult:
        return ComputeCostResult(
            total_cost_usd=_accrued_cost(job, as_of),
            measured_at=as_of,
        )

    async def terminate(self, job: ComputeJob, *, reason: str) -> bool:
        handle = self._processes.pop(job.external_compute_id or "", None)
        if handle is not None:
            if handle.proc.poll() is not None:
                return True
            pid = handle.proc.pid
        else:
            pid = await asyncio.to_thread(_local_subprocess_pid, job)
            if pid is None or not await _aio_is_pid_running(pid):
                return True
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        return True


class GcpComputeBackend:
    name = GCP_BACKEND_NAME

    def external_id_for(self, job: ComputeJob) -> str:
        return f"compute-job-vm-{job.id.hex[:12]}"

    async def launch(self, spec: ComputeLaunchSpec) -> ComputeLaunchResult:
        external_id = spec.external_id or f"compute-job-vm-{spec.job_id.hex[:12]}"
        artifact_uri = storage.compute_job_artifact_uri(spec.job_id)
        # Idempotent re-launch: if the reserved VM already exists (a crash between
        # the durable STARTING commit and this point), adopt it instead of
        # provisioning a second VM and double-spending the budget.
        existing_zone = await asyncio.to_thread(compute.find_vm_zone, external_id)
        if existing_zone:
            started_at = await asyncio.to_thread(
                compute.get_vm_created_at, external_id, existing_zone
            )
            return ComputeLaunchResult(
                external_id=external_id,
                started_at=started_at or _utcnow(),
                cost_rate_per_second=0.0,
                message="adopted existing compute-job VM",
                # The adopted VM was provisioned by this backend, so its startup
                # script embeds the HMAC heartbeat token just like the normal
                # path below. Stamp the marker so legacy UUID-bearer heartbeats
                # are rejected for the crash-recovered job too — otherwise anyone
                # knowing the job UUID could spoof its heartbeats.
                metadata={
                    "zone": existing_zone,
                    "artifact_uri": artifact_uri,
                    "heartbeat_auth": "hmac",
                },
            )
        runtime_budget_seconds = spec.runtime_deadline_seconds
        if runtime_budget_seconds is not None:
            artifact_expiry_base = _utcnow() + timedelta(
                seconds=max(0, int(runtime_budget_seconds))
            )
        else:
            artifact_expiry_base = spec.deadline_at
        artifact_expiry_at = _artifact_upload_expiry(artifact_expiry_base)
        artifact_upload_url = (
            await storage.generate_compute_job_artifact_upload_signed_url(
                spec.job_id,
                expires_at=artifact_expiry_at,
            )
        )
        # Cross-job checkpoint resume: mint a short-lived signed GET for the
        # predecessor's durable bundle so the VM can pull + extract it into the
        # artifact dir before the command runs. The resume source was validated at
        # create_compute_job time (same project, durable bundle present).
        input_artifact_download_url: str | None = None
        if spec.input_artifact_job_id:
            input_artifact_download_url = (
                await storage.generate_compute_job_artifact_download_signed_url(
                    spec.input_artifact_job_id,
                    expires_at=artifact_expiry_at,
                )
            )
        ok = await compute.provision_ephemeral_vm(
            external_id,
            startup_script=_build_gcp_startup_script(
                spec,
                artifact_upload_url=artifact_upload_url,
                input_artifact_download_url=input_artifact_download_url,
            ),
            machine_type=spec.machine_type,
            provisioning_model=spec.provisioning_model,
            accelerator=spec.accelerator,
            accelerator_count=spec.accelerator_count,
        )
        if not ok:
            raise RuntimeError("GCP compute-job VM provisioning failed")
        zone = await asyncio.to_thread(compute.get_vm_zone, external_id)
        metadata: Dict[str, Any] = {
            "artifact_uri": artifact_uri,
            # Durable per-job marker: this VM's startup script embeds the HMAC
            # heartbeat token, so legacy UUID-bearer heartbeats must be rejected.
            "heartbeat_auth": "hmac",
        }
        if spec.provisioning_deadline_at is not None:
            metadata["provisioning_deadline_at"] = (
                spec.provisioning_deadline_at.isoformat()
            )
        if spec.runtime_deadline_seconds is not None:
            metadata["runtime_deadline_seconds"] = spec.runtime_deadline_seconds
        if zone and zone != "unknown":
            metadata["zone"] = zone
        return ComputeLaunchResult(
            external_id=external_id,
            started_at=_utcnow(),
            cost_rate_per_second=0.0,
            message="compute-job VM provisioning started",
            metadata=metadata,
        )

    async def poll_status(self, job: ComputeJob) -> ComputeStatusResult:
        external_id = job.external_compute_id or ""
        if not external_id:
            return ComputeStatusResult(
                status=ComputeJobStatus.STARTING,
                message="compute-job VM not yet launched",
            )
        zone = await asyncio.to_thread(_resolve_job_zone, job)
        # All GCP RPCs are synchronous; off-load to a thread so a slow serial /
        # instance-status call can't stall the event loop and unrelated services.
        serial = await asyncio.to_thread(
            compute.get_vm_serial_output, external_id, zone
        )
        serial_status = _parse_serial_status(serial or "")
        if serial_status is not None:
            return serial_status

        if not zone:
            return ComputeStatusResult(
                status=ComputeJobStatus.STARTING,
                message="waiting for compute-job VM zone",
            )
        vm_status = await asyncio.to_thread(compute.get_vm_status, external_id, zone)
        if vm_status in {"RUNNING", "STAGING"}:
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
        if vm_status in {"PROVISIONING"}:
            return ComputeStatusResult(
                status=ComputeJobStatus.STARTING,
                message="compute-job VM still provisioning",
            )
        if vm_status in {"TERMINATED", "STOPPING", "DELETED"}:
            # The serial marker was absent (checked above), but a fast job can
            # power off before its serial SUCCEEDED/FAILED marker is captured.
            # Its authoritative outcome still lands durably in terminal.json
            # inside the GCS artifact bundle — reconcile from it before declaring
            # a blind FAILURE; only fall through when no durable record exists.
            reconciled = await _reconcile_from_durable_terminal_record(job)
            if reconciled is not None:
                return reconciled
            return ComputeStatusResult(
                status=ComputeJobStatus.FAILED,
                message="compute-job VM stopped without a terminal marker",
                finished_at=_utcnow(),
            )
        return ComputeStatusResult(
            status=ComputeJobStatus.STARTING,
            message="waiting for compute-job VM status",
        )

    async def poll_cost(self, job: ComputeJob, *, as_of: datetime) -> ComputeCostResult:
        return ComputeCostResult(
            total_cost_usd=_accrued_cost(job, as_of),
            measured_at=as_of,
        )

    async def terminate(self, job: ComputeJob, *, reason: str) -> bool:
        external_id = job.external_compute_id or ""
        if not external_id:
            return True
        # Give the guest a graceful stop window so the startup script can flush
        # its artifact bundle before the instance is deleted.
        await compute.stop_vm(external_id, reason=f"compute_job_{reason}")
        # _ensure_vm_deleted records the orchestrator-initiated-delete marker
        # before its first GCP call, so the reaper never misclassifies an
        # intentional budget/deadline teardown as a spot preemption. It returns
        # False if the VM survived all retries; surface that so the monitor can
        # flag the row — the orphan sweep (compute-job-vm-* aware) is the safety
        # net that eventually reclaims a VM that outlived its immediate delete.
        return await _ensure_vm_deleted(external_id, reason=f"compute_job_{reason}")


class LocalGPUComputeBackend:
    """Local subprocess backend that injects GPU metadata into launch results.

    Delegates all execution to LocalSubprocessComputeBackend; the only
    difference is that detect_local_gpu() output is stamped into the
    launch result metadata so callers can see what GPU substrate was used.
    """

    name = LOCAL_GPU_BACKEND_NAME

    def __init__(self) -> None:
        self._delegate = LocalSubprocessComputeBackend()

    def external_id_for(self, job: ComputeJob) -> str:
        return self._delegate.external_id_for(job)

    async def launch(self, spec: ComputeLaunchSpec) -> ComputeLaunchResult:
        result = await self._delegate.launch(spec)
        gpu_info = await asyncio.to_thread(detect_local_gpu)
        if gpu_info is not None:
            result.metadata["local_gpu"] = gpu_info
        return result

    async def poll_status(self, job: ComputeJob) -> ComputeStatusResult:
        return await self._delegate.poll_status(job)

    async def poll_cost(
        self, job: ComputeJob, *, as_of: datetime
    ) -> ComputeCostResult:
        return await self._delegate.poll_cost(job, as_of=as_of)

    async def terminate(self, job: ComputeJob, *, reason: str) -> bool:
        return await self._delegate.terminate(job, reason=reason)


class RunnerSubprocessBackend:
    """Passthrough backend for compute jobs running on operator hardware.

    ``launch()`` is a no-op: the job enters STARTING and waits for a runner
    to claim it via ``POST /api/runner/next-compute-job``.  ``poll_status()``
    reads runner-reported execution state from ``polling_metadata``, stamped
    by ``POST /api/runner/compute-job/{id}/report``.  ``poll_cost()`` returns
    zero (operator hardware has no cloud billing).  ``terminate()`` returns
    True; the job is finalized by ``_apply_task_terminal_cancellation`` and the
    runner observes the terminal state on its next report.
    """

    name = RUNNER_SUBPROCESS_BACKEND_NAME

    def external_id_for(self, job: ComputeJob) -> str:
        return f"runner-{job.id}"

    async def launch(self, spec: ComputeLaunchSpec) -> ComputeLaunchResult:
        metadata: dict = {}
        if spec.repo_clone:
            # Persist owner/repo/ref so /next-compute-job can return it to the
            # runner for git clone.  The token is NOT stored here — it is minted
            # fresh at claim time to avoid persisting a short-lived secret.
            metadata["repo_clone"] = spec.repo_clone
        return ComputeLaunchResult(
            external_id=spec.external_id or f"runner-{spec.job_id}",
            started_at=_utcnow(),
            cost_rate_per_second=0.0,
            message="runner-subprocess: awaiting runner claim",
            metadata=metadata,
        )

    async def poll_status(self, job: ComputeJob) -> ComputeStatusResult:
        meta = job.polling_metadata or {}
        runner_exec_status = meta.get("runner_exec_status")
        if runner_exec_status is None:
            # Runner has not yet reported — still waiting for claim/first report.
            return ComputeStatusResult(status=ComputeJobStatus.STARTING)
        if runner_exec_status == "running":
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
        if runner_exec_status in ("succeeded", "failed"):
            raw_code = meta.get("runner_exit_code")
            exit_code = (
                raw_code
                if isinstance(raw_code, int) and not isinstance(raw_code, bool)
                else None
            )
            raw_metrics = meta.get("runner_result_metrics")
            metrics = raw_metrics if isinstance(raw_metrics, dict) else None
            raw_log = meta.get("runner_command_log")
            command_log = raw_log if isinstance(raw_log, dict) else None
            raw_ts = meta.get("runner_finished_at")
            finished_at = None
            if isinstance(raw_ts, str):
                try:
                    from datetime import datetime as _dt
                    finished_at = _dt.fromisoformat(
                        raw_ts.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except (ValueError, AttributeError):
                    pass
            cjs = (
                ComputeJobStatus.SUCCEEDED
                if runner_exec_status == "succeeded"
                else ComputeJobStatus.FAILED
            )
            if cjs == ComputeJobStatus.SUCCEEDED:
                message = "compute job succeeded"
            elif exit_code == NO_PROGRESS_WATCHDOG_EXIT_CODE:
                # No-progress watchdog kill on the runner. The runner /report
                # endpoint carries only exec_status/exit_code (no reason field),
                # so translate the reserved exit code here into a terminal reason
                # string classify_compute_terminal_reason keys on. Without this the
                # kill collapses to CODE_ERROR (burns retry budget, misattributes
                # blame) instead of NO_PROGRESS_TIMEOUT — matching the GCP/local
                # -subprocess paths which emit REASON=no_progress_timeout directly.
                message = "no_progress_timeout"
            else:
                message = "compute job failed"
            return ComputeStatusResult(
                status=cjs,
                exit_code=exit_code if exit_code is not None else (0 if cjs == ComputeJobStatus.SUCCEEDED else None),
                finished_at=finished_at or _utcnow(),
                metrics=metrics,
                command_log=command_log,
                message=message,
            )
        # Unknown status string — treat as still running.
        return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

    async def poll_cost(
        self, job: ComputeJob, *, as_of: datetime
    ) -> ComputeCostResult:
        # Operator-hardware nominal accrual. Reporting a flat $0 here zeroed the
        # cost/task denominator for every runner-subprocess (GPU) job AND made the
        # spend_cap a no-op — the monitor terminates on ``accrued_cost_usd >=
        # spend_cap_usd`` (compute_jobs.py), which never advanced while cost was 0.
        # Accrue a small nominal rate over elapsed runtime so the cap bounds
        # runtime and the job's cost is non-null. Monotonic: never below the value
        # already persisted on the row. ``as_of`` is the authoritative stop time
        # (the monitor passes ``finished_at`` for a terminal job), so a finished
        # job is never billed past the moment it ended.
        prior = float(job.accrued_cost_usd or 0.0)
        if (
            job.started_at is None
            or OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND <= 0
        ):
            return ComputeCostResult(total_cost_usd=prior, measured_at=as_of)
        elapsed = max(0.0, (as_of - job.started_at).total_seconds())
        nominal = elapsed * OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND
        return ComputeCostResult(
            total_cost_usd=max(prior, nominal), measured_at=as_of
        )

    async def terminate(self, job: ComputeJob, *, reason: str) -> bool:
        # The runner observes the cancel signal when it gets back cancel_requested=True
        # from the /report endpoint (job is CANCELLED after _apply_task_terminal_cancellation).
        return True


_LOCAL_BACKEND = LocalSubprocessComputeBackend()
_GCP_BACKEND = GcpComputeBackend()
_LOCAL_GPU_BACKEND = LocalGPUComputeBackend()
_RUNNER_SUBPROCESS_BACKEND = RunnerSubprocessBackend()


def get_compute_backend(name: str) -> ComputeBackend:
    if name == _LOCAL_BACKEND.name:
        return _LOCAL_BACKEND
    if name == _GCP_BACKEND.name:
        return _GCP_BACKEND
    if name == _LOCAL_GPU_BACKEND.name:
        return _LOCAL_GPU_BACKEND
    if name == _RUNNER_SUBPROCESS_BACKEND.name:
        return _RUNNER_SUBPROCESS_BACKEND
    raise ValueError(f"unknown compute backend: {name}")


def default_compute_backend_name() -> str:
    return _LOCAL_BACKEND.name if compute.LOCAL_MODE else _GCP_BACKEND.name


def validate_backend_name(name: str) -> str:
    """Validate a backend name is known AND runnable in the current mode.

    Rejecting at create time prevents wedged jobs: an unknown backend, or "gcp"
    forced under LOCAL_MODE (or the local backend in a cloud deployment), would
    otherwise persist a row that the monitor can never launch — it would raise on
    every pass and roll back to its pending state forever.
    """
    backend = get_compute_backend(name)  # raises ValueError on unknown name
    if compute.LOCAL_MODE and backend is _GCP_BACKEND:
        raise ValueError("gcp backend is unavailable in LOCAL_MODE")
    if not compute.LOCAL_MODE and backend is _LOCAL_BACKEND:
        raise ValueError("local-subprocess backend is unavailable outside LOCAL_MODE")
    if backend is _LOCAL_GPU_BACKEND:
        if detect_local_gpu() is None:
            raise ValueError(
                "local-gpu backend is unavailable: no local GPU detected"
            )
    # runner-subprocess is available in both LOCAL_MODE and cloud mode — its
    # availability depends on runner registration, not the leader's deploy mode.
    return name


def _resolve_job_zone(job: ComputeJob) -> str | None:
    """Restart-safe zone lookup for a compute-job VM.

    The persisted ``polling_metadata['zone']`` survives a leader restart (the
    in-memory ``compute._vm_zones`` map does not), so prefer it; fall back to the
    in-memory map and finally an aggregated cross-zone discovery.
    """
    persisted = (job.polling_metadata or {}).get("zone")
    if persisted:
        return str(persisted)
    return compute.find_vm_zone(job.external_compute_id or "")


def _local_subprocess_pid(job: ComputeJob) -> int | None:
    raw_pid = (job.polling_metadata or {}).get("pid")
    if raw_pid in (None, ""):
        return _local_subprocess_pid_for_external_id(job.external_compute_id)
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return _local_subprocess_pid_for_external_id(job.external_compute_id)
    return pid if pid > 0 else None


def _local_subprocess_pid_for_external_id(external_id: str | None) -> int | None:
    raw_pid = _read_local_state_text(_local_pid_path_for_external_id(external_id))
    if raw_pid in (None, ""):
        return None
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _local_subprocess_started_at_for_external_id(
    external_id: str | None,
) -> datetime | None:
    raw_started_at = _read_local_state_text(
        _local_started_at_path_for_external_id(external_id)
    )
    if not raw_started_at:
        return None
    try:
        parsed = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _read_local_terminal_status(job: ComputeJob) -> ComputeStatusResult | None:
    return _read_local_terminal_status_for_external_id(job.external_compute_id)


def _read_local_terminal_status_for_external_id(
    external_id: str | None,
) -> ComputeStatusResult | None:
    raw = _read_local_state_text(_local_terminal_path_for_external_id(external_id))
    if raw is None:
        return None
    data: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    raw_status = data.get("STATUS")
    if raw_status not in {"SUCCEEDED", "FAILED"}:
        return None
    exit_code: int | None
    try:
        exit_code = int(data["EXIT_CODE"]) if "EXIT_CODE" in data else None
    except ValueError:
        exit_code = None
    raw_finished_at = data.get("FINISHED_AT")
    finished_at = _utcnow()
    if raw_finished_at:
        try:
            parsed = datetime.fromisoformat(raw_finished_at.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            finished_at = parsed
        except ValueError:
            finished_at = _utcnow()
    reason = data.get("REASON") or None
    metrics = _decode_result_metrics(data.get("METRICS"))
    command_log = _decode_command_log(data.get("COMMAND_LOG"))
    echoed_release_sha = _decode_release_echo_sha(data.get("RELEASE_ECHO"))
    if raw_status == "SUCCEEDED":
        return ComputeStatusResult(
            status=ComputeJobStatus.SUCCEEDED,
            message="local subprocess exited 0",
            exit_code=0 if exit_code is None else exit_code,
            finished_at=finished_at,
            metrics=metrics,
            command_log=command_log,
            echoed_release_sha=echoed_release_sha,
        )
    if exit_code is None:
        return ComputeStatusResult(
            status=ComputeJobStatus.FAILED,
            message=reason or "local subprocess exited with unknown code",
            finished_at=finished_at,
            metrics=metrics,
            command_log=command_log,
            echoed_release_sha=echoed_release_sha,
        )
    return ComputeStatusResult(
        status=ComputeJobStatus.FAILED,
        message=reason or f"local subprocess exited with code {exit_code}",
        exit_code=exit_code,
        finished_at=finished_at,
        metrics=metrics,
        command_log=command_log,
        echoed_release_sha=echoed_release_sha,
    )


def _build_container_run_prefix(
    image: str,
    *,
    host_out_dir: str,
    container_out_dir: str = _CONTAINER_OUT_DIR,
    gpus: bool = False,
    extra_env: dict[str, str] | None = None,
    extra_mounts: list[tuple[str, str]] | None = None,
    workdir: str | None = None,
    name: str | None = None,
) -> str:
    """Return a shell prefix: ``docker run --rm [--name N] [--gpus all] -v … -e … IMAGE``.

    The caller appends ``bash -lc <quoted-command>`` after this prefix.
    ``host_out_dir`` must exist on the host before docker run is called;
    it is bind-mounted at ``container_out_dir`` so the command's
    result.json and artifacts land at the same host paths the terminal-status
    reader already uses. ``extra_mounts`` adds further ``host:container`` bind
    mounts (e.g. the cloned repo) and ``workdir`` sets the in-container cwd.
    ``name`` sets a deterministic ``--name`` so an out-of-band killer (the
    no-progress watchdog) can ``docker kill`` the container: a containerized
    trainer is parented by dockerd, NOT a member of the wrapper's process group,
    so a group signal never reaches it — reaching it needs the container id/name.
    """
    result_file = f"{container_out_dir}/result.json"
    artifact_dir = f"{container_out_dir}/artifacts"
    progress_file = f"{container_out_dir}/progress.json"
    parts = ["docker", "run", "--rm"]
    if name:
        parts += ["--name", shlex.quote(name)]
    if gpus:
        parts += ["--gpus", "all"]
    parts += [
        "-v", shlex.quote(f"{host_out_dir}:{container_out_dir}"),
        "-e", shlex.quote(f"{RESULT_FILE_ENV}={result_file}"),
        "-e", shlex.quote(f"{ARTIFACT_DIR_ENV}={artifact_dir}"),
        "-e", shlex.quote(f"{PROGRESS_FILE_ENV}={progress_file}"),
    ]
    for host_path, container_path in extra_mounts or []:
        parts += ["-v", shlex.quote(f"{host_path}:{container_path}")]
    for k, v in (extra_env or {}).items():
        parts += ["-e", shlex.quote(f"{k}={v}")]
    if workdir:
        parts += ["-w", shlex.quote(workdir)]
    parts.append(shlex.quote(image))
    return " ".join(parts)


def _build_local_wrapper_command(
    command: str,
    *,
    external_id: str,
    setup_step: str | None = None,
    container_image: str | None = None,
    container_registry_auth: dict | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    repo_clone: dict | None = None,
    no_progress_timeout_seconds: int | None = None,
) -> str:
    state_dir = shlex.quote(_local_state_dir_for_external_id(external_id))
    pid_path = shlex.quote(_local_pid_path_for_external_id(external_id))
    terminal_path = shlex.quote(_local_terminal_path_for_external_id(external_id))
    result_path = shlex.quote(_local_result_path_for_external_id(external_id))
    artifact_dir = shlex.quote(_local_artifact_dir_for_external_id(external_id))
    progress_file = shlex.quote(_local_progress_file_path_for_external_id(external_id))
    command_log_path = shlex.quote(
        os.path.join(
            _local_artifact_dir_for_external_id(external_id),
            "command.log.json",
        )
    )
    live_tail_path_local = shlex.quote(
        os.path.join(
            _local_artifact_dir_for_external_id(external_id),
            "live_tail.b64",
        )
    )
    # Block emitted before the python runner that handles docker login + pull.
    # Separated from the run command so pull failure can write a typed terminal
    # reason (container_image_pull_failed) instead of falling through as a
    # generic command failure.
    container_setup_block = ""
    # Deterministic container name so the no-progress watchdog can ``docker kill``
    # the dockerd-parented workload (which the group SIGKILL can never reach).
    # None for a bare (non-containerized) job — the group kill suffices there.
    container_name: str | None = None
    if container_image:
        container_name = _local_container_name_for_external_id(external_id)
        host_out_dir = _local_state_dir_for_external_id(external_id)
        gpus = detect_local_gpu() is not None
        # When the contract includes a repo checkout, bind-mount the repo root
        # (the running deployment's checkout) into the container as /workspace so
        # in-container imports resolve the same way they do on the bare substrate.
        _container_extra_mounts: list[tuple[str, str]] | None = None
        _container_workdir: str | None = None
        _container_extra_env: dict[str, str] | None = None
        if repo_clone:
            _repo_root = str(Path(__file__).parent.parent.parent)
            _container_extra_mounts = [(_repo_root, "/workspace")]
            _container_workdir = "/workspace"
            _container_extra_env = {"PYTHONPATH": "/workspace"}
            if env:
                _container_extra_env.update(env)
        else:
            _container_extra_env = dict(env) if env else None
            if cwd:
                _container_workdir = cwd
        prefix = _build_container_run_prefix(
            container_image,
            host_out_dir=host_out_dir,
            gpus=gpus,
            extra_env=_container_extra_env,
            extra_mounts=_container_extra_mounts,
            workdir=_container_workdir,
            name=container_name,
        )
        effective_command = f"{prefix} bash -lc {shlex.quote(command)}"
        auth = container_registry_auth or {}
        pwd = str(auth.get("password") or "")
        login_line = ""
        if pwd:
            server_q = shlex.quote(str(auth.get("server") or ""))
            user_q = shlex.quote(str(auth.get("username") or ""))
            login_line = (
                f"printf '%s' {shlex.quote(pwd)} | "
                f"docker login {server_q} -u {user_q} --password-stdin >/dev/null 2>&1\n"
            )
        image_q = shlex.quote(container_image)
        container_setup_block = (
            login_line
            + f"if ! docker pull {image_q} >/dev/null 2>&1; then\n"
            + "  FINISHED_AT=$(date -u +'%Y-%m-%dT%H:%M:%SZ')\n"
            + f"  cat > {terminal_path} <<PULL_EOF\n"
            + "STATUS=FAILED\n"
            + "EXIT_CODE=1\n"
            + "FINISHED_AT=$FINISHED_AT\n"
            + "REASON=container_image_pull_failed\n"
            + "METRICS=\n"
            + "COMMAND_LOG=\n"
            + "RELEASE_ECHO=\n"
            + "PULL_EOF\n"
            + "  exit 1\n"
            + "fi"
        )
    else:
        effective_command = command
    command_literal = shlex.quote(effective_command)
    # Optional setup step: run before the main command; on failure write the
    # terminal file and exit so the main command never starts.
    setup_step_block = ""
    if setup_step:
        setup_step_log = shlex.quote(
            os.path.join(
                _local_artifact_dir_for_external_id(external_id),
                "setup_step.log",
            )
        )
        setup_step_q = shlex.quote(setup_step)
        setup_step_block = f"""bash -lc {setup_step_q} >> {setup_step_log} 2>&1; SETUP_EXIT=$?
if [ "$SETUP_EXIT" -ne 0 ]; then
  FINISHED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  cat > {terminal_path} <<SETUP_EOF
STATUS=FAILED
EXIT_CODE=$SETUP_EXIT
FINISHED_AT=$FINISHED_AT
METRICS=
COMMAND_LOG=
RELEASE_ECHO=
SETUP_EOF
  exit "$SETUP_EXIT"
fi"""
    # The command runner is the same real, lintable file the GCP startup script
    # ships (backend/app/_compute_scripts/command_runner.py), delivered as a
    # base64 blob so no character has to survive an escape pass — per the
    # "Generating Code for One Language From Another" rule, never a multi-line
    # heredoc of another language. It is decoded into the job's private state
    # dir (not a shared /tmp path) so concurrent local jobs cannot race on it.
    runner_path = shlex.quote(
        os.path.join(
            _local_state_dir_for_external_id(external_id), "command_runner.py"
        )
    )
    # The artifact dir is created but NOT removed: its contents are the durable
    # output channel and must survive the subprocess (and an idempotent relaunch).
    _no_progress_watchdog_block = ""
    _np_watchdog_cleanup = ""
    if no_progress_timeout_seconds is not None and no_progress_timeout_seconds > 0:
        _np_seconds = int(no_progress_timeout_seconds)
        # For a CONTAINERIZED job the trainer runs as a child of dockerd, NOT a
        # member of this wrapper's process group, so the group SIGKILL below can
        # never reach it — a TERM-ignoring trainer would keep running (and keep
        # billing) after the job was recorded FAILED. `docker kill <name>`
        # (SIGKILL to the container's PID 1) is the only signal path that actually
        # stops the dockerd-parented workload. Emitted on its own line so the
        # shared kill-ordering guard still sees the bare `kill -TERM`/`kill -KILL`
        # lines intact; a no-op (empty) for a bare job where the group kill reaches
        # everything. `|| true` tolerates a container that never started (e.g. the
        # window fired mid-`docker pull`).
        if container_name is not None:
            _np_container_kill = (
                f"      docker kill {shlex.quote(container_name)} 2>/dev/null || true\n"
            )
        else:
            _np_container_kill = ""
        _no_progress_watchdog_block = f"""# No-progress watchdog: kills the job if the progress file is untouched
# for {_np_seconds}s. Runs in parallel with the command as a SLIDING window —
# each interval that advances the progress mtime resets the deadline, so a job
# that progresses then stalls is still cut on the next quiet interval (matches
# the GCP startup-script and scripts/local_runner.py watchdogs).
touch {progress_file} 2>/dev/null || true
(
  _NP_START=$(stat -c %Y {progress_file} 2>/dev/null || echo 0)
  while true; do
    sleep {_np_seconds}
    _NP_NOW=$(stat -c %Y {progress_file} 2>/dev/null || echo 0)
    if [ "$_NP_NOW" = "$_NP_START" ]; then
      # No progress in the last window — terminal reason no_progress_timeout.
      FINISHED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
      cat > {terminal_path} <<NP_EOF
STATUS=FAILED
EXIT_CODE={NO_PROGRESS_WATCHDOG_EXIT_CODE}
FINISHED_AT=$FINISHED_AT
REASON=no_progress_timeout
METRICS=
COMMAND_LOG=
RELEASE_ECHO=
NP_EOF
      # The watchdog runs in the SAME process group it is about to signal (it is
      # a background subshell of the job's group leader, $$). Without this trap
      # the group SIGTERM below lands on the watchdog itself, killing it instantly
      # — so the SIGKILL escalation two lines down was unreachable and a command
      # that ignores/blocks SIGTERM (a trap handler, an uninterruptible
      # docker-exec'd trainer) simply kept running, and kept billing, after the
      # watchdog had already written the no_progress_timeout terminal record.
      # Ignoring TERM *only here* (never in the outer wrapper) lets the watchdog
      # outlive the TERM it sends, wait out the grace window, and then land the
      # SIGKILL that actually reaps the group. The SIGKILL reaps this subshell
      # too — kill(2) delivers to every member of the group before the caller
      # dies — which is fine: its job is done by then.
      trap '' TERM
      kill -TERM -$$ 2>/dev/null || true
      sleep 2
{_np_container_kill}      kill -KILL -$$ 2>/dev/null || true
      exit 0
    fi
    _NP_START=$_NP_NOW
  done
) &
_NP_WATCHDOG_PID=$!
"""
        _np_watchdog_cleanup = 'kill "$_NP_WATCHDOG_PID" 2>/dev/null || true'
    # NOTE on the failure branch below: `EXIT_CODE=$?` MUST come before
    # `STATUS=FAILED`. This block previously assigned STATUS first, so `$?` read
    # the *assignment's* exit status (always 0) instead of python3's — every failing
    # local/runner job recorded EXIT_CODE=0 and the real exit code, the only
    # evidence of HOW the command failed, was silently lost. The GCP startup script
    # already gets this order right; this path did not.
    return f"""mkdir -p {state_dir}
mkdir -p {artifact_dir}
printf '%s\\n' "$$" > {pid_path}
rm -f {terminal_path}
rm -f {result_path}
rm -f {command_log_path}
echo '{_COMMAND_RUNNER_B64}' | base64 -d > {runner_path}
export {RESULT_FILE_ENV}={result_path}
export {ARTIFACT_DIR_ENV}={artifact_dir}
export {PROGRESS_FILE_ENV}={progress_file}
{setup_step_block}
{container_setup_block}
{_no_progress_watchdog_block}
EXIT_CODE=0
if python3 {runner_path} \
  {command_literal} \
  {command_log_path} \
  {_MAX_COMMAND_LOG_BYTES} \
  {live_tail_path_local}; then
  STATUS=SUCCEEDED
  EXIT_CODE=0
else
  # Order is load-bearing: read $? before any other command runs.
  EXIT_CODE=$?
  STATUS=FAILED
fi
{_np_watchdog_cleanup}
FINISHED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
METRICS_B64=""
if [ -f {result_path} ]; then
  METRICS_B64=$(base64 -w0 < {result_path} 2>/dev/null || true)
fi
COMMAND_LOG_B64=""
if [ -s {command_log_path} ]; then
  COMMAND_LOG_B64=$(base64 -w0 < {command_log_path} 2>/dev/null || true)
fi
RELEASE_ECHO_B64=""
if [ -n "${{BLUEPRINT_RELEASE_SHA:-}}" ]; then
  RELEASE_ECHO_B64=$(printf '%s' "${{BLUEPRINT_RELEASE_SHA}}" | base64 -w0 2>/dev/null || true)
fi
cat > {terminal_path} <<EOF
STATUS=$STATUS
EXIT_CODE=$EXIT_CODE
FINISHED_AT=$FINISHED_AT
METRICS=$METRICS_B64
COMMAND_LOG=$COMMAND_LOG_B64
RELEASE_ECHO=$RELEASE_ECHO_B64
EOF
exit "$EXIT_CODE"
"""


def _local_state_dir_for_external_id(external_id: str | None) -> str:
    suffix = str(external_id or "unassigned")
    return os.path.join(_LOCAL_STATE_ROOT, suffix)


def _local_container_name_for_external_id(external_id: str | None) -> str:
    """Deterministic docker ``--name`` for a local containerized job's container.

    The no-progress watchdog needs a stable handle to ``docker kill`` the
    container: the trainer inside runs as a child of dockerd, NOT a member of the
    wrapper's process group, so the watchdog's group SIGKILL can never reach it —
    a TERM-ignoring trainer would keep running (and keep billing) after the job
    was recorded FAILED. Naming the container lets the watchdog reach the real,
    dockerd-parented workload. Sanitized to the docker name grammar
    (``[a-zA-Z0-9][a-zA-Z0-9_.-]*``) so any external_id yields a valid name.
    """
    raw = f"blueprint-compute-{external_id or 'unassigned'}"
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)
    # docker requires the first char to be alphanumeric; the constant prefix
    # already guarantees that, but stay defensive.
    return safe if safe[:1].isalnum() else f"c{safe}"


def _local_pid_path_for_external_id(external_id: str | None) -> str:
    return os.path.join(
        _local_state_dir_for_external_id(external_id), _LOCAL_PID_FILENAME
    )


def _local_started_at_path_for_external_id(external_id: str | None) -> str:
    return os.path.join(
        _local_state_dir_for_external_id(external_id),
        _LOCAL_STARTED_AT_FILENAME,
    )


def _local_terminal_path_for_external_id(external_id: str | None) -> str:
    return os.path.join(
        _local_state_dir_for_external_id(external_id),
        _LOCAL_TERMINAL_FILENAME,
    )


def _local_result_path_for_external_id(external_id: str | None) -> str:
    return os.path.join(
        _local_state_dir_for_external_id(external_id),
        _LOCAL_RESULT_FILENAME,
    )


def _local_artifact_dir_for_external_id(external_id: str | None) -> str:
    """Durable on-disk artifact directory for a local job.

    Lives under the job's state root (in the system temp dir), so its contents
    outlive the subprocess and remain re-readable after exit/restart — the local
    analogue of the GCP per-job object prefix.
    """
    return os.path.join(
        _local_state_dir_for_external_id(external_id),
        _LOCAL_ARTIFACT_DIRNAME,
    )


def _local_artifact_metadata(external_id: str | None) -> dict[str, str]:
    """Durable artifact-location metadata persisted into ``polling_metadata``.

    ``artifact_dir`` is the raw on-disk path (convenient for re-reads/tests);
    ``artifact_uri`` is the canonical durable location key both backends populate.
    """
    path = _local_artifact_dir_for_external_id(external_id)
    return {"artifact_dir": path, "artifact_uri": f"file://{path}"}


def _local_progress_file_path_for_external_id(external_id: str | None) -> str:
    """Progress file path for a local job — polled by the no-progress watchdog."""
    return os.path.join(
        _local_state_dir_for_external_id(external_id),
        "progress.json",
    )


def _read_local_live_tail(job: ComputeJob) -> dict[str, Any] | None:
    """Read live_tail.b64 from a running local job's artifact dir, if present."""
    meta = job.polling_metadata or {}
    artifact_dir = str(meta.get("artifact_dir") or "").strip()
    if not artifact_dir:
        artifact_dir = _local_artifact_dir_for_external_id(job.external_compute_id)
    live_tail_path = Path(artifact_dir) / "live_tail.b64"
    try:
        data = live_tail_path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
        parsed = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    text = parsed.get("text")
    truncated = parsed.get("truncated")
    if not isinstance(text, str) or not isinstance(truncated, bool):
        return None
    return {"text": text, "truncated": truncated}


async def collect_terminal_artifact_reference(
    job: ComputeJob,
) -> TerminalArtifactReference | None:
    """Best-effort durable-artifact detection for a terminal compute job.

    Returns a reference to the job's durable artifact location IFF that location
    holds at least one object, else ``None``. The detection is backend-aware:
    the local backend inspects the on-disk artifact directory that survives the
    subprocess; the GCP backend lists the per-job object prefix the startup
    script uploaded to before poweroff; the runner-subprocess backend rehydrates
    the ``runner_artifact`` stamp the receiving endpoint wrote from
    :func:`store_runner_artifact`.

    Fail-soft by contract: an empty artifact directory, a missing location, or
    ANY detection error degrades to ``None`` (interpreted upstream as 'no
    artifact') and never raises — artifact linkage must never block a job's
    terminal completion.
    """
    try:
        if job.backend in (LOCAL_BACKEND_NAME, LOCAL_GPU_BACKEND_NAME):
            return await asyncio.to_thread(_collect_local_artifact_reference, job)
        if job.backend == GCP_BACKEND_NAME:
            return await _collect_gcp_artifact_reference(job)
        if job.backend == RUNNER_SUBPROCESS_BACKEND_NAME:
            return await asyncio.to_thread(_collect_runner_artifact_reference, job)
    except Exception:
        logger.warning(
            "Durable-artifact detection failed for compute job %s",
            getattr(job, "id", None),
            exc_info=True,
        )
    return None


def _collect_local_artifact_reference(
    job: ComputeJob,
) -> TerminalArtifactReference | None:
    meta = job.polling_metadata or {}
    artifact_dir = str(meta.get("artifact_dir") or "").strip()
    if not artifact_dir:
        artifact_dir = _local_artifact_dir_for_external_id(job.external_compute_id)
    file_count = _count_files_in_tree(artifact_dir)
    if file_count <= 0:
        return None
    uri = str(meta.get("artifact_uri") or "").strip() or f"file://{artifact_dir}"
    return TerminalArtifactReference(
        uri=uri,
        backend=job.backend,
        file_count=file_count,
        total_size_bytes=_compute_tree_size_bytes(artifact_dir),
    )


async def _collect_gcp_artifact_reference(
    job: ComputeJob,
) -> TerminalArtifactReference | None:
    meta = job.polling_metadata or {}
    artifact_uri = str(meta.get("artifact_uri") or "").strip()
    if not artifact_uri:
        artifact_uri = storage.compute_job_artifact_uri(job.id)
    file_count = await storage.count_compute_job_artifacts(job.id)
    if file_count <= 0:
        return None
    total_size_bytes = await storage.get_compute_job_artifact_total_bytes(job.id)
    return TerminalArtifactReference(
        uri=artifact_uri,
        backend=GCP_BACKEND_NAME,
        file_count=file_count,
        total_size_bytes=total_size_bytes,
    )


def _collect_runner_artifact_reference(
    job: ComputeJob,
) -> TerminalArtifactReference | None:
    """Build a terminal-artifact reference for a runner-subprocess job.

    Unlike the local/GCP branches, the runner has no in-process or VM-side
    location for the orchestrator to inspect: the runner tars its artifact dir
    and POSTs the bytes to the receiving endpoint, which calls
    :func:`store_runner_artifact` and stamps the returned dict onto
    ``polling_metadata["runner_artifact"]``. This branch simply rehydrates that
    stamp into the shared :class:`TerminalArtifactReference` shape the other
    branches return.

    Mirrors the other branches' emptiness contract: a reference is produced ONLY
    for a non-empty bundle (``file_count >= 1``); an absent stamp or an empty
    bundle degrades to ``None`` ('no artifact'). ``uri`` is the durable
    ``gs://`` location when the upload succeeded, or ``None`` in the degraded
    no-store path (the durable file-count/byte metrics are still surfaced so the
    job's terminal completion and metric-based criteria are unaffected, while a
    ``require_durable_artifact`` gate correctly stays unmet without a durable
    location).
    """
    meta = job.polling_metadata or {}
    ref = meta.get("runner_artifact")
    if not isinstance(ref, dict):
        return None
    try:
        file_count = int(ref.get("file_count") or 0)
        total_size_bytes = int(ref.get("total_bytes") or 0)
    except (TypeError, ValueError):
        return None
    if file_count <= 0:
        return None
    uri = ref.get("uri")
    return TerminalArtifactReference(
        uri=uri if isinstance(uri, str) and uri.strip() else None,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        file_count=file_count,
        total_size_bytes=total_size_bytes,
    )


async def store_runner_artifact(
    session,
    job: ComputeJob,
    archive_bytes: bytes,
    file_count: int,
    total_bytes: int,
) -> dict[str, Any]:
    """Durably persist a runner-subprocess job's artifact tarball.

    Mirrors the GCP backend's durable-artifact store exactly: the gzip tarball
    the runner uploaded is written to the SAME per-job object the GCE startup
    script uploads to before poweroff —
    ``compute-jobs/<job_id>/artifacts/bundle.tar.gz`` in the shared artifact
    bucket (:data:`storage.ARTIFACT_BUCKET_NAME`). Reusing
    :func:`storage.compute_job_artifact_object_name` /
    :func:`storage.compute_job_artifact_uri` means the bundle is retrievable
    through the identical helpers the GCP path already uses
    (``storage.download_compute_job_artifact_bundle``); no new store, bucket, or
    path convention is introduced.

    The orchestrator (Cloud Run) writes with its own ADC — the same credential
    the GCP path already uses to list/download/IAM the bucket — so this works
    from the request context that receives the runner upload (the VM-side signed
    URL is unnecessary here).

    ``session`` is accepted for caller-contract symmetry; this function does not
    touch the DB. The caller stamps the returned dict onto
    ``job.polling_metadata["runner_artifact"]``.

    Returns a reference dict::

        {"kind": "runner_artifact", "uri": <gs://...|None>,
         "file_count": int, "total_bytes": int, "stored": bool}

    Graceful degradation: when no durable object store is reachable (local dev
    without GCS credentials, or any network/permission/decode error), the upload
    is skipped best-effort and the dict carries ``uri=None, stored=False``. The
    job still completes and its durable metrics are still stamped from
    ``file_count``/``total_bytes``. NEVER raises — durable-artifact storage must
    not block a runner job's terminal completion.
    """
    file_count = max(int(file_count or 0), 0)
    total_bytes = max(int(total_bytes or 0), 0)
    reference: dict[str, Any] = {
        "kind": "runner_artifact",
        "uri": None,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "stored": False,
    }
    if not archive_bytes:
        return reference

    object_name = storage.compute_job_artifact_object_name(job.id)
    bucket_name = storage.ARTIFACT_BUCKET_NAME
    project_id = storage.GCP_PROJECT_ID

    def _upload() -> None:
        client = gcs_storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        # upload_from_string streams the already-in-memory bytes once; it does
        # not duplicate the buffer, so a large (within-cap) bundle stays bounded.
        blob.upload_from_string(archive_bytes, content_type="application/gzip")

    try:
        await asyncio.to_thread(_upload)
    except Exception:
        logger.warning(
            "Durable runner-artifact upload failed for compute job %s; "
            "completing with no durable store (uri=None)",
            getattr(job, "id", None),
            exc_info=True,
        )
        return reference

    reference["uri"] = storage.compute_job_artifact_uri(job.id)
    reference["stored"] = True
    return reference


@dataclass(slots=True)
class ViewableArtifactCandidate:
    """One viewable file selected from a terminal job's artifacts for per-file store.

    ``filename`` is the sanitized object name derived from the file's path
    *relative* to the artifact root (path-traversal safe, subdir context folded
    to ``__`` so multi-arm same-named plots stay distinct),
    ``content_type`` / ``kind`` come from the extension classifier, and
    ``size_bytes`` is the file's byte length.

    Exactly one payload source is set:

      * ``data`` — the file's bytes held in memory (images, and small videos
        within ``_MAX_VIEWABLE_FILE_BYTES``). Uploaded via ``upload_from_string``.
      * ``source_path`` — an on-disk path to STREAM (large videos over the
        in-memory cap). Uploaded via ``upload_from_file`` so the bytes never
        enter the orchestrator's heap. ``source_is_temp`` marks a temp file this
        pipeline owns and must unlink after upload; ``artifact_root`` (when set)
        is the local job's artifact root, so the upload path re-verifies symlink
        containment and opens the original file ``O_NOFOLLOW`` (TOCTOU guard).
    """

    filename: str
    content_type: str
    kind: str
    size_bytes: int
    data: bytes | None = None
    source_path: str | None = None
    source_is_temp: bool = False
    artifact_root: str | None = None


def _classify_viewable_member(
    name: str, size: int
) -> tuple[str, str, str] | None:
    """Return ``(sanitized_relname, content_type, kind)`` for a viewable archive
    member, or ``None`` when it does not qualify.

    The single classification rule shared by every viewable selection pass (the
    in-memory extractor and the streaming two-pass selector): viewable extension,
    positive size within the per-kind cap (images ≤ ``_MAX_VIEWABLE_FILE_BYTES``,
    videos ≤ ``_MAX_VIEWABLE_VIDEO_BYTES`` since they stream), and a traversal-safe
    sanitized *relative* path (an absolute/``..`` member path sanitizes to
    ``None`` and is skipped).
    """
    classified = storage.viewable_content_type_for_filename(name)
    if classified is None:
        return None
    content_type, kind = classified
    if size <= 0 or size > _viewable_size_cap_for_kind(kind):
        return None
    relname = storage.sanitize_viewable_relpath(name)
    if relname is None:
        return None
    return relname, content_type, kind


def _read_capped_member(
    tar: tarfile.TarFile, member: tarfile.TarInfo, cap: int = _MAX_VIEWABLE_FILE_BYTES
) -> bytes | None:
    """Read ONE archive member's bytes, refusing anything over ``cap``."""
    extracted = tar.extractfile(member)
    if extracted is None:
        return None
    data = extracted.read(cap + 1)
    if not data or len(data) > cap:
        return None
    return data


def _extract_member_to_tempfile(
    tar: tarfile.TarFile, member: tarfile.TarInfo
) -> str | None:
    """STREAM one archive member's bytes to a fresh temp file; return its path.

    For large videos that must never be materialized in the orchestrator's heap:
    the member is decompressed and copied in bounded chunks (``copyfileobj``), so
    peak memory is the copy buffer, not the file size. Returns the temp-file path
    (caller owns it and must unlink), or ``None`` on any error (temp file is
    cleaned up before returning).
    """
    extracted = tar.extractfile(member)
    if extracted is None:
        return None
    handle, tmp_path = tempfile.mkstemp(prefix="bp-viewable-vid-", suffix=".bin")
    try:
        with os.fdopen(handle, "wb") as dest:
            shutil.copyfileobj(extracted, dest)
        return tmp_path
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None


def _unlink_temp_sources(paths: list[str]) -> None:
    """Best-effort unlink of pipeline-owned temp files; never raises."""
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _download_artifact_bundle_to_path(job_id, dest_path: str) -> bool:
    """STREAM a job's durable ``bundle.tar.gz`` to *dest_path* on disk.

    The memory-safe counterpart of ``storage.download_compute_job_artifact_bundle``
    (which returns the whole object as one ``bytes`` — fine for the small
    ``terminal.json`` recovery read, an OOM risk for a multi-GB results bundle).
    ``blob.download_to_file`` writes the object through in chunks, so the
    orchestrator's peak memory is bounded by ``chunk_size``, not by the bundle
    size. Returns False when the object does not exist. Raises on transport
    errors — the caller (``_stream_viewable_candidates_from_bundle``) is the
    fail-soft boundary.
    """
    client = gcs_storage.Client(project=storage.GCP_PROJECT_ID)
    bucket = client.bucket(storage.ARTIFACT_BUCKET_NAME)
    blob = bucket.blob(storage.compute_job_artifact_object_name(job_id))
    if not blob.exists():
        return False
    blob.chunk_size = _BUNDLE_DOWNLOAD_CHUNK_BYTES
    with open(dest_path, "wb") as handle:
        blob.download_to_file(handle)
    return True


def _select_viewable_candidates_from_bundle_path(
    path: str,
    *,
    fail_soft: bool = True,
) -> tuple[list[ViewableArtifactCandidate], int]:
    """Select a bundle's viewable files from an ON-DISK archive, bounded memory.

    Two passes over the (seekable) archive file:

      1. **Metadata pass** — classify every member by name/size ONLY. No member
         bytes are read, so an archive with thousands of images costs nothing but
         its member headers. Yields the full qualifying set, sorted by sanitized
         name, which is then capped at ``_MAX_VIEWABLE_FILE_COUNT``.
      2. **Data pass** — materialize ONLY the selected members: images (and
         small videos) are read into memory under the in-memory cap; large
         videos over that cap are STREAMED member-by-member to a temp file so
         they never enter the heap.

    Capping BEFORE reading is what bounds peak memory: the selected set can never
    exceed ``_MAX_VIEWABLE_FILE_COUNT`` objects, no in-memory member exceeds
    ``_MAX_VIEWABLE_FILE_BYTES``, and streamed videos add only the copy buffer —
    nothing else about the bundle (its total size, its non-viewable payload, its
    count of rejected images) is ever resident. Returns ``(selected, dropped)``
    with the same contract as the in-memory extractor's caller.

    Defensive by contract: a non-gzip/non-tar file or ANY parse error yields
    ``([], 0)``; any temp files created for streamed videos are unlinked on error.
    """
    tmp_video_paths: list[str] = []
    try:
        refs: list[tuple[str, str, str, str]] = []  # (relname, member, ctype, kind)
        seen: set[str] = set()
        size_dropped = 0
        with tarfile.open(path, mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                classified = _classify_viewable_member(member.name, member.size)
                if classified is None:
                    # Count a viewable-extension member omitted SOLELY for the
                    # per-file size cap so the omitted tally is accurate; a plain
                    # non-viewable member is not counted.
                    if (
                        storage.viewable_content_type_for_filename(member.name)
                        is not None
                        and member.size > _MAX_VIEWABLE_FILE_BYTES
                    ):
                        size_dropped += 1
                    continue
                relname, content_type, kind = classified
                if relname in seen:
                    continue
                seen.add(relname)
                refs.append((relname, member.name, content_type, kind))

        refs.sort(key=lambda ref: ref[0])
        selected_refs = refs[:_MAX_VIEWABLE_FILE_COUNT]
        dropped = (len(refs) - len(selected_refs)) + size_dropped
        if not selected_refs:
            return [], dropped

        wanted = {ref[1]: ref for ref in selected_refs}
        out: dict[str, ViewableArtifactCandidate] = {}
        with tarfile.open(path, mode="r:gz") as tar:
            for member in tar:
                if not member.isfile() or member.name not in wanted:
                    continue
                relname, _member_name, content_type, kind = wanted[member.name]
                if relname in out:
                    continue  # first occurrence wins (matches the metadata pass)
                if kind == "video" and member.size > _MAX_VIEWABLE_FILE_BYTES:
                    tmp_path = _extract_member_to_tempfile(tar, member)
                    if tmp_path is None:
                        continue
                    tmp_video_paths.append(tmp_path)
                    try:
                        streamed_size = os.path.getsize(tmp_path)
                    except OSError:
                        continue
                    out[relname] = ViewableArtifactCandidate(
                        filename=relname,
                        content_type=content_type,
                        kind=kind,
                        size_bytes=streamed_size,
                        source_path=tmp_path,
                        source_is_temp=True,
                    )
                else:
                    data = _read_capped_member(
                        tar, member, _viewable_size_cap_for_kind(kind)
                    )
                    if data is None:
                        continue
                    out[relname] = ViewableArtifactCandidate(
                        filename=relname,
                        content_type=content_type,
                        kind=kind,
                        size_bytes=len(data),
                        data=data,
                    )
                if len(out) == len(selected_refs):
                    break
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        _unlink_temp_sources(tmp_video_paths)
        if not fail_soft:
            raise RuntimeError("viewable bundle enumeration failed") from exc
        return [], 0
    if len(out) != len(selected_refs) and not fail_soft:
        _unlink_temp_sources(tmp_video_paths)
        raise RuntimeError("viewable bundle enumeration was incomplete")
    # Unlink any temp video that did NOT become a surviving candidate (e.g. a
    # post-extract stat failed) so it can't leak; the rest are unlinked by
    # persist_viewable_artifacts after upload.
    kept = {c.source_path for c in out.values() if c.source_path is not None}
    _unlink_temp_sources([p for p in tmp_video_paths if p not in kept])
    return sorted(out.values(), key=lambda c: c.filename), dropped


async def _stream_viewable_candidates_from_bundle(
    job_id,
    *,
    fail_soft: bool = True,
) -> tuple[list[ViewableArtifactCandidate], int]:
    """Fetch + select a bundle-backed job's viewable files without ever holding
    the bundle in memory.

    The bundle is streamed to a temp file, selected from on disk (two-pass,
    count-capped BEFORE any member bytes are read), and the temp file is always
    removed. Fail-soft: a missing bundle or ANY download/parse error degrades to
    ``([], 0)``.
    """
    handle, tmp_path = tempfile.mkstemp(prefix="bp-viewable-", suffix=".tar.gz")
    os.close(handle)
    try:
        downloaded = await asyncio.to_thread(
            _download_artifact_bundle_to_path, job_id, tmp_path
        )
        if not downloaded:
            if not fail_soft:
                raise RuntimeError("durable viewable bundle is unavailable")
            return [], 0
        return await asyncio.to_thread(
            _select_viewable_candidates_from_bundle_path,
            tmp_path,
            fail_soft=fail_soft,
        )
    except Exception:
        logger.warning(
            "Failed to stream durable artifact bundle for compute job %s; "
            "completing with no viewable manifest",
            job_id,
            exc_info=True,
        )
        if not fail_soft:
            raise
        return [], 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _extract_viewable_candidates_from_bundle(
    raw: bytes,
) -> list[ViewableArtifactCandidate]:
    """Select viewable files from an IN-MEMORY ``.tar.gz`` bundle (no extract-to-disk).

    The in-memory analogue of :func:`_select_viewable_candidates_from_bundle_path`,
    for callers that already hold the bundle bytes. The durable-bundle backends
    (gcp / runner-subprocess) do NOT use this — a results bundle can be
    arbitrarily large and materializing it in the orchestrator is an OOM — they
    go through :func:`_stream_viewable_candidates_from_bundle`.

    Mirrors the safe path-matching pattern in
    :func:`_extract_terminal_record_from_bundle`: iterate members, classify by
    the file extension, and read each selected member's bytes directly from the
    archive. The object name is derived from the sanitized *relative* member path
    (via :func:`storage.sanitize_viewable_relpath`), NOT the bare basename, so a
    multi-arm sweep whose arms each write a same-named plot
    (``arm1/reward.png`` vs ``arm2/reward.png``) yields distinct candidates
    instead of collapsing to one. Enforces the per-file size cap, dedupes by that
    sanitized relpath (first occurrence wins), and classifies each as
    ``image``/``video``. The total-count cap is NOT applied here — every
    qualifying file is returned so the caller can sort deterministically before
    capping; :func:`_collect_viewable_artifact_candidates` enforces the count cap
    and reports the dropped count. A traversal/absolute member path
    sanitizes to ``None`` and is skipped. Defensive by contract: a non-gzip/
    non-tar blob or ANY parse error yields ``[]``.
    """
    if not raw:
        return []
    out: list[ViewableArtifactCandidate] = []
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                classified = _classify_viewable_member(member.name, member.size)
                if classified is None:
                    continue
                relname, content_type, kind = classified
                if relname in seen:
                    continue
                # The whole bundle is already resident here, so materializing one
                # member (image or video) under its per-kind cap adds no new peak.
                data = _read_capped_member(
                    tar, member, _viewable_size_cap_for_kind(kind)
                )
                if data is None:
                    continue
                seen.add(relname)
                out.append(
                    ViewableArtifactCandidate(
                        filename=relname,
                        content_type=content_type,
                        kind=kind,
                        size_bytes=len(data),
                        data=data,
                    )
                )
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return []
    return out


def _is_under_artifact_root(real_path: str, real_root: str) -> bool:
    """True iff *real_path* (already realpath'd) is *real_root* or inside it."""
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def _read_viewable_file_nofollow(path: str, artifact_root: str) -> bytes | None:
    """Read a regular file for preview persistence, refusing to follow symlinks.

    Re-checks ``islink`` + realpath containment at read time and opens with
    ``O_NOFOLLOW`` where the platform supports it, closing the scan→read
    TOCTOU window (an entry swapped for a symlink after the scan is refused
    by the kernel, not just by the earlier stat). Returns ``None`` on any
    refusal/error — callers skip the file.
    """
    if os.path.islink(path):
        return None
    if not _is_under_artifact_root(os.path.realpath(path), artifact_root):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as file_handle:
            return file_handle.read(_MAX_VIEWABLE_FILE_BYTES + 1)
    except OSError:
        return None


def _collect_viewable_candidates_from_dir(
    artifact_dir: str,
) -> tuple[list[ViewableArtifactCandidate], int]:
    """Select viewable files by walking a local job's on-disk artifact dir.

    The on-disk analogue of :func:`_extract_viewable_candidates_from_bundle`:
    same viewable-extension filter, per-file size cap, and total-count cap. The
    object name (and dedupe key) is the sanitized path *relative to*
    ``artifact_dir`` (via :func:`storage.sanitize_viewable_relpath`), NOT the
    bare basename, so same-named plots under different arm subdirectories
    (``arm1/reward.png`` vs ``arm2/reward.png``) stay distinct instead of
    collapsing to one. Returns ``(candidates, size_dropped)`` where
    ``size_dropped`` is the count of viewable-extension files skipped SOLELY for
    exceeding the per-file byte cap (``_MAX_VIEWABLE_FILE_BYTES``) — so the
    caller can surface an accurate omitted tally. Returns ``([], 0)`` for a
    missing/empty/unreadable dir or on ANY OS error — never raises.

    SECURITY — symlink exfiltration guard: the artifact dir was writable by the
    experiment command, and this scan runs on the HOST after the run — a
    planted symlink (e.g. ``loss_curve.png -> /etc/passwd`` or an operator
    credentials file) must never be dereferenced and persisted as a
    viewer-accessible preview. Symlinks are excluded at every layer:
    ``followlinks=False`` on the walk, symlinked dirs pruned, symlinked entries
    skipped, a realpath containment check against the artifact root, and an
    ``O_NOFOLLOW`` read (:func:`_read_viewable_file_nofollow`) so an entry
    swapped for a symlink between scan and read (TOCTOU) is refused by the
    kernel, not just by the earlier stat.
    """
    if (
        not artifact_dir
        or not os.path.isdir(artifact_dir)
        or os.path.islink(artifact_dir)
    ):
        return [], 0
    artifact_root = os.path.realpath(artifact_dir)
    out: list[ViewableArtifactCandidate] = []
    size_dropped = 0
    seen: set[str] = set()
    try:
        for root, dirs, files in os.walk(artifact_dir, followlinks=False):
            dirs[:] = [
                d for d in dirs if not os.path.islink(os.path.join(root, d))
            ]
            for name in sorted(files):
                classified = storage.viewable_content_type_for_filename(name)
                if classified is None:
                    continue
                path = os.path.join(root, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                if not _is_under_artifact_root(
                    os.path.realpath(path), artifact_root
                ):
                    continue
                relpath = os.path.relpath(path, artifact_dir)
                relname = storage.sanitize_viewable_relpath(relpath)
                if relname is None or relname in seen:
                    continue
                content_type, kind = classified
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size <= 0:
                    continue
                if size > _viewable_size_cap_for_kind(kind):
                    # Viewable file omitted SOLELY for the per-file size cap —
                    # tally it (deduped by relname) so the count reaches the UI.
                    seen.add(relname)
                    size_dropped += 1
                    continue
                if kind == "video" and size > _MAX_VIEWABLE_FILE_BYTES:
                    # Large video: STREAM the original file at upload time (never
                    # buffer it here). The upload path re-verifies containment and
                    # opens O_NOFOLLOW via ``artifact_root`` to keep the symlink
                    # TOCTOU guard closed.
                    seen.add(relname)
                    out.append(
                        ViewableArtifactCandidate(
                            filename=relname,
                            content_type=content_type,
                            kind=kind,
                            size_bytes=size,
                            source_path=path,
                            source_is_temp=False,
                            artifact_root=artifact_root,
                        )
                    )
                    continue
                data = _read_viewable_file_nofollow(path, artifact_root)
                if not data or len(data) > _MAX_VIEWABLE_FILE_BYTES:
                    continue
                seen.add(relname)
                out.append(
                    ViewableArtifactCandidate(
                        filename=relname,
                        content_type=content_type,
                        kind=kind,
                        size_bytes=len(data),
                        data=data,
                    )
                )
    except OSError:
        return out, size_dropped
    return out, size_dropped


async def _collect_viewable_artifact_candidates(
    job: ComputeJob,
    *,
    fail_soft: bool = True,
) -> tuple[list[ViewableArtifactCandidate], int]:
    """Backend-aware selection of a terminal job's viewable files.

    Local/local-gpu jobs are read from the on-disk artifact dir that survives the
    subprocess; gcp and runner-subprocess jobs are read from the durable
    ``bundle.tar.gz`` — STREAMED to a temp file and selected from disk, never
    materialized in the orchestrator's heap (a results bundle has no size bound;
    pulling one into the API process is an OOM). Returns ``([], 0)`` for an
    unrecognised backend or a missing bundle.

    Candidates are sorted by sanitized filename for deterministic selection when
    the archive/walk order is arbitrary. Returns ``(selected, dropped)`` where
    ``dropped`` is the total count of viewable files omitted from the manifest —
    both those exceeding ``_MAX_VIEWABLE_FILE_COUNT`` (count-cap overflow) AND
    those skipped for exceeding ``_MAX_VIEWABLE_FILE_BYTES`` (size-cap).
    """
    if job.backend in (LOCAL_BACKEND_NAME, LOCAL_GPU_BACKEND_NAME):
        meta = job.polling_metadata or {}
        artifact_dir = str(meta.get("artifact_dir") or "").strip()
        if not artifact_dir:
            artifact_dir = _local_artifact_dir_for_external_id(job.external_compute_id)
        all_candidates, size_dropped = await asyncio.to_thread(
            _collect_viewable_candidates_from_dir, artifact_dir
        )
    elif job.backend in (GCP_BACKEND_NAME, RUNNER_SUBPROCESS_BACKEND_NAME):
        # Already sorted + capped on disk (count cap applied BEFORE any member
        # bytes are read; size-cap omissions folded into the returned count), so
        # it returns the final (selected, dropped) directly.
        return await _stream_viewable_candidates_from_bundle(
            job.id,
            fail_soft=fail_soft,
        )
    else:
        return [], 0
    all_candidates.sort(key=lambda c: c.filename)
    selected = all_candidates[:_MAX_VIEWABLE_FILE_COUNT]
    count_dropped = len(all_candidates) - len(selected)
    return selected, count_dropped + size_dropped


async def persist_viewable_artifacts(
    job: ComputeJob,
) -> tuple[list[dict[str, Any]], int]:
    """Compatibility wrapper returning the historical ``(manifest, dropped)``."""
    manifest, dropped, _complete = (
        await persist_viewable_artifacts_with_completeness(job)
    )
    return manifest, dropped


async def persist_viewable_artifacts_with_completeness(
    job: ComputeJob,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Persist a terminal job's viewable files as individually-addressable objects.

    Extracts the viewable files (png/jpg/jpeg/webp/gif/mp4/webm — NO svg, which
    can execute script when served from a signed URL) from the
    job's artifacts, uploads each under the allowlisted per-job prefix, and
    returns ``(manifest, dropped, complete)`` — a list of
    ``{filename, storage_uri, content_type, size_bytes, kind}`` for durably stored
    files, and the count of qualifying files that exceeded ``_MAX_VIEWABLE_FILE_COUNT``
    and were not persisted. ``complete`` is false when the durable bundle could
    not be enumerated fully or any selected file failed to persist.

    Fail-soft by contract: an empty/corrupt bundle, a missing dir, or ANY
    extraction/upload error degrades to ``([], 0)`` and NEVER raises,
    so viewable-artifact persistence can never block a job's terminal completion.
    Individual per-file upload failures are skipped; only durably-stored files
    appear in the returned manifest.
    """
    try:
        candidates, dropped = await _collect_viewable_artifact_candidates(
            job,
            fail_soft=False,
        )
    except Exception:
        logger.warning(
            "Viewable-artifact extraction failed for compute job %s",
            getattr(job, "id", None),
            exc_info=True,
        )
        return [], 0, False
    if not candidates:
        return [], dropped, True
    manifest: list[dict[str, Any]] = []
    complete = True
    for candidate in candidates:
        try:
            uri = await _upload_viewable_candidate(job, candidate)
        except Exception:
            logger.warning(
                "Viewable-artifact upload raised for compute job %s file %r",
                getattr(job, "id", None),
                candidate.filename,
                exc_info=True,
            )
            uri = None
        finally:
            # A temp file this pipeline created for a streamed video is only ours
            # to remove; a local job's original artifact file must stay in place.
            if candidate.source_is_temp and candidate.source_path:
                _unlink_temp_sources([candidate.source_path])
        if not uri:
            complete = False
            continue
        manifest.append(
            {
                "filename": candidate.filename,
                "storage_uri": uri,
                "content_type": candidate.content_type,
                "size_bytes": candidate.size_bytes,
                "kind": candidate.kind,
                # The object URI is deterministic and may have held a live
                # generation already. Terminal persistence is an authoritative
                # overwrite, so it receives a fresh cache identity.
                "revision": storage.new_viewable_revision(),
                "live": False,
            }
        )
    return manifest, dropped, complete


def _open_viewable_source_nofollow(path: str, artifact_root: str | None):
    """Open a streamed video candidate's source for upload; ``None`` on refusal.

    For a temp file this pipeline owns (``artifact_root is None``) a plain open is
    safe. For a local job's ORIGINAL artifact file, re-verify symlink containment
    and open with ``O_NOFOLLOW`` so an entry swapped for a symlink between the
    selection scan and the upload (TOCTOU) is refused by the kernel — the same
    guard :func:`_read_viewable_file_nofollow` applies to in-memory reads.
    """
    if artifact_root is None:
        try:
            return open(path, "rb")
        except OSError:
            return None
    if os.path.islink(path):
        return None
    if not _is_under_artifact_root(os.path.realpath(path), artifact_root):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        return os.fdopen(fd, "rb")
    except OSError:
        return None


async def _upload_viewable_candidate(
    job: ComputeJob, candidate: ViewableArtifactCandidate
) -> str | None:
    """Upload one candidate, choosing the in-memory or streaming storage seam.

    Images (and small videos) carry their bytes in ``data`` and go through the
    in-memory ``upload_compute_job_viewable_file``. Large videos carry a
    ``source_path`` and are STREAMED via ``upload_compute_job_viewable_stream``,
    so their bytes never enter the orchestrator's heap. Returns the durable URI
    or ``None`` (never raises for a missing/unreadable source).
    """
    if candidate.source_path is not None:
        fileobj = await asyncio.to_thread(
            _open_viewable_source_nofollow,
            candidate.source_path,
            candidate.artifact_root,
        )
        if fileobj is None:
            return None
        try:
            return await storage.upload_compute_job_viewable_stream(
                job.id,
                candidate.filename,
                fileobj,
                candidate.content_type,
                size=candidate.size_bytes,
            )
        finally:
            try:
                fileobj.close()
            except OSError:
                pass
    return await storage.upload_compute_job_viewable_file(
        job.id,
        candidate.filename,
        candidate.data,
        candidate.content_type,
    )


def _count_files_in_tree(path: str) -> int:
    """Count regular files anywhere under ``path`` (0 if missing/empty/unreadable)."""
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for _root, _dirs, files in os.walk(path):
        total += len(files)
    return total


def _compute_tree_size_bytes(path: str) -> int:
    """Sum the byte sizes of all regular files under ``path`` (0 if unreadable)."""
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _read_local_state_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as file_handle:
            return file_handle.read().strip()
    except OSError:
        return None


def _write_local_state_text(path: str, value: str) -> None:
    with open(path, "w", encoding="utf-8") as file_handle:
        file_handle.write(value)


def _remove_local_terminal_status_for_external_id(external_id: str | None) -> None:
    try:
        os.remove(_local_terminal_path_for_external_id(external_id))
    except FileNotFoundError:
        return


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    proc_stat_path = f"/proc/{pid}/stat"
    try:
        with open(proc_stat_path, encoding="utf-8") as proc_stat:
            fields = proc_stat.read().split()
    except OSError:
        return True
    return len(fields) < 3 or fields[2] != "Z"


async def _aio_is_pid_running(pid: int) -> bool:
    return await asyncio.to_thread(_is_pid_running, pid)


def _accrued_cost(job: ComputeJob, as_of: datetime) -> float:
    """Cost accrued from ``started_at`` to ``as_of`` at the persisted rate.

    ``as_of`` is the authoritative stop time — for a terminal job the monitor
    passes ``finished_at`` so the job is never billed past the moment it ended.
    """
    if job.started_at is None:
        return float(job.accrued_cost_usd or 0.0)
    rate = float((job.polling_metadata or {}).get("cost_rate_per_second") or 0.0)
    elapsed = max(0.0, (as_of - job.started_at).total_seconds())
    return max(float(job.accrued_cost_usd or 0.0), elapsed * rate)


def _artifact_upload_expiry(deadline_at: datetime | None) -> datetime:
    """Self-expiry for the compute-job artifact upload URL.

    Set comfortably past the job's deadline so the on-VM upload (which runs right
    before poweroff, after the command finishes at/near the deadline) still has a
    live URL, while bounding the lifetime of the upload credential if teardown
    never runs.
    """
    base = deadline_at or (_utcnow() + timedelta(hours=1))
    return base + timedelta(hours=1)


def _build_gcp_startup_script(
    spec: ComputeLaunchSpec,
    *,
    artifact_upload_url: str,
    input_artifact_download_url: str | None = None,
) -> str:
    env_lines = []
    for key, value in sorted((spec.env or {}).items()):
        # Defense-in-depth mirror of the contract-time ENV_KEY_RE gate. The key
        # is interpolated BARE into `export {key}=...` and this script runs
        # WITHOUT `set -e`, so a key like "my-model" makes bash error ("not a
        # valid identifier") and then SILENTLY CONTINUE — burning a GPU on a run
        # that is missing that env var.
        #
        # fullmatch, not match: `$` also matches before a trailing newline, so
        # `.match("GOOD\n")` would pass this guard and emit `export GOOD\n=v` —
        # two lines, `export GOOD` (empty) then `=v` (command not found), and
        # with no `set -e` the billed run continues without the var. That is the
        # exact silent degradation this guard exists to stop, so it must not be
        # reachable through the guard itself.
        #
        # Raise rather than skip: dropping the key would still launch the very
        # run this guard exists to prevent (missing env var, full GPU spend),
        # just with a warning nobody reads. This runs BEFORE provision_ephemeral_vm,
        # so the raise costs no VM, and ComputeLaunchSpecError tells compute_jobs
        # to terminalize the job FAILED right away with this message — which names
        # the offending key — as the operator-visible terminal_reason.
        if not ENV_KEY_RE.fullmatch(str(key)):
            raise ComputeLaunchSpecError(
                f"compute job {spec.job_id}: env key {key!r} is not a valid "
                f"shell identifier (must match {ENV_KEY_RE.pattern}) — refusing "
                "to launch a billed run whose `export` would fail silently"
            )
        env_lines.append(f"export {key}={shlex.quote(value)}")
    env_block = "\n".join(env_lines)
    cwd_line = f"cd {shlex.quote(spec.cwd)}" if spec.cwd else ""
    # Quoted on-VM paths the durable terminal record and the clone-fail branch
    # both reference, so assign them before repo_setup_block is built below.
    result_file = shlex.quote(_GCP_RESULT_FILE_PATH)
    artifact_dir = shlex.quote(_GCP_ARTIFACT_DIR_PATH)
    progress_file = shlex.quote(_GCP_PROGRESS_FILE_PATH)
    # Container mode: when the contract declares a runtime image, the command runs
    # inside `docker run` instead of a bare `bash -lc`. The VM artifact dir is
    # bind-mounted into the container so result.json and artifacts written inside
    # land on the host where the terminal-record reader and artifact upload already
    # look. result.json therefore moves from the standalone _GCP_RESULT_FILE_PATH
    # to a sibling of the mounted artifact dir; every host reference below uses the
    # `result_file` variable, so reassigning it here keeps the boot-beacon, TTL, and
    # completion records pointed at the real file. Absent container_image ⇒ this
    # whole block is skipped and the generated script is byte-for-byte unchanged.
    container_run_prefix = ""
    if spec.container_image:
        container_result_host_path = os.path.join(
            _GCP_ARTIFACT_DIR_PATH, "result.json"
        )
        result_file = shlex.quote(container_result_host_path)
        # In container mode _build_container_run_prefix points the container's
        # BLUEPRINT_PROGRESS_FILE at container_out_dir/progress.json. For GCP
        # host_out_dir == container_out_dir == _GCP_ARTIFACT_DIR_PATH, so the
        # command writes progress to _GCP_ARTIFACT_DIR_PATH/progress.json on the
        # host — a different file from the standalone _GCP_PROGRESS_FILE_PATH the
        # bare-mode export uses. Repoint the host-visible progress_file (which the
        # no-progress watchdog stat's) at that same file so the watchdog observes
        # the container's mtime advances instead of killing a live job at the
        # first no_progress_timeout window.
        container_progress_host_path = os.path.join(
            _GCP_ARTIFACT_DIR_PATH, "progress.json"
        )
        progress_file = shlex.quote(container_progress_host_path)
        _container_extra_mounts: list[tuple[str, str]] | None = None
        _container_workdir: str | None = None
        _container_extra_env: dict[str, str] | None = None
        if spec.repo_clone:
            # The repo is cloned to /tmp/blueprint_repo on the VM (see
            # repo_setup_block); mount it into the container and run there so the
            # command's imports resolve exactly as they do on the bare substrate.
            _container_extra_mounts = [("/tmp/blueprint_repo", "/workspace")]
            _container_workdir = "/workspace"
            _container_extra_env = {"PYTHONPATH": "/workspace"}
            if spec.env:
                _container_extra_env.update(spec.env)
        else:
            _container_extra_env = dict(spec.env) if spec.env else None
            _container_workdir = spec.cwd or None
        container_run_prefix = _build_container_run_prefix(
            spec.container_image,
            host_out_dir=_GCP_ARTIFACT_DIR_PATH,
            container_out_dir=_GCP_ARTIFACT_DIR_PATH,
            gpus=spec.accelerator is not None,
            extra_mounts=_container_extra_mounts,
            extra_env=_container_extra_env,
            workdir=_container_workdir,
        )
    command_log_path = shlex.quote(
        os.path.join(_GCP_ARTIFACT_DIR_PATH, "command.log.json")
    )
    live_tail_path = shlex.quote(
        os.path.join(_GCP_ARTIFACT_DIR_PATH, "live_tail.b64")
    )
    # Optional repo checkout: clone the project repo at the requested ref into a
    # scratch dir, cd into it, and put it on PYTHONPATH so the command can import
    # the project's packages. Runs BEFORE cwd_line (an explicit cwd still wins).
    # A clone failure terminates the job LOUDLY with a parseable reason instead of
    # letting the command die on ModuleNotFound and leaving "no terminal marker".
    # The clone is silenced (>/dev/null 2>&1) so the install token never reaches
    # the serial console.
    repo_setup_block = ""
    if spec.repo_clone and spec.repo_token:
        _repo_dir_q = shlex.quote("/tmp/blueprint_repo")
        _ref = str(spec.repo_clone.get("ref") or "main")
        _owner = str(spec.repo_clone.get("owner") or "")
        _repo = str(spec.repo_clone.get("repo") or "")
        _clone_url = (
            f"https://x-access-token:{spec.repo_token}@github.com/{_owner}/{_repo}.git"
        )
        # The clone's stderr is captured to a file (not discarded) so a FAILURE is
        # ATTRIBUTABLE — bare "repo_clone_failed" hid whether a 4 MB clone of a
        # branch that exists died on auth, a drained GitHub quota, a network blip,
        # or a timeout. We classify the cause from that file, scrub any token out of
        # it, then fold the scrubbed text into the durable bundle. The serial
        # console only ever sees the TYPED reason (repo_clone_failed:<class>) — the
        # captured stderr (which can echo the tokened URL) never reaches it.
        _clone_err_q = shlex.quote("/tmp/blueprint_clone.err")
        _clone_err_artifact = shlex.quote(
            os.path.join(_GCP_ARTIFACT_DIR_PATH, "clone.err")
        )
        repo_setup_block = (
            f"rm -rf {_repo_dir_q}\n"
            f"if ! git clone --depth 1 --branch {shlex.quote(_ref)} "
            f"{shlex.quote(_clone_url)} {_repo_dir_q} >/dev/null 2>{_clone_err_q}; then\n"
            f"  CLONE_CLASS=unknown\n"
            f"  if grep -qiE 'rate limit|too many requests|429' {_clone_err_q} "
            f"2>/dev/null; then CLONE_CLASS=ratelimit\n"
            f"  elif grep -qiE 'authentication failed|invalid username or password|"
            f"could not read from remote|repository not found|access denied|403|401' "
            f"{_clone_err_q} 2>/dev/null; then CLONE_CLASS=auth\n"
            f"  elif grep -qiE 'could not resolve host|name or service not known|"
            f"connection refused|network is unreachable|failed to connect|"
            f"could not connect' {_clone_err_q} 2>/dev/null; then CLONE_CLASS=network\n"
            f"  elif grep -qiE 'timed out|timeout' {_clone_err_q} 2>/dev/null; "
            f"then CLONE_CLASS=timeout\n"
            f"  fi\n"
            f"  sed -i -E 's#x-access-token:[^@]*@#x-access-token:REDACTED@#g; "
            f"s#gh[a-z]_[A-Za-z0-9]{{20,}}#REDACTED#g' {_clone_err_q} "
            f"2>/dev/null || true\n"
            f"  cp {_clone_err_q} {_clone_err_artifact} 2>/dev/null || true\n"
            f'  echo "{_SERIAL_REASON_PREFIX}repo_clone_failed:$CLONE_CLASS"\n'
            f'  echo "{_SERIAL_STATUS_PREFIX}FAILED"\n'
            f'  echo "{_SERIAL_EXIT_PREFIX}1"\n'
            f"  python3 /tmp/blueprint_terminal_record.py "
            f"{artifact_dir} FAILED 1 repo_clone_failed:$CLONE_CLASS "
            f"{result_file} {command_log_path} >/dev/null 2>&1 || true\n"
            f"  upload_artifacts\n"
            f'  kill "$TTL_PID" "$_NP_WATCHDOG_PID" "$_VIEWABLE_PID" 2>/dev/null || true\n'
            f"  poweroff || shutdown -h now || true\n"
            f"  exit 1\n"
            f"fi\n"
            f"cd {_repo_dir_q}\n"
            f"export PYTHONPATH={_repo_dir_q}:${{PYTHONPATH:-}}"
        )
    if spec.runtime_deadline_seconds is not None:
        deadline_seconds = max(60, int(spec.runtime_deadline_seconds) + 30)
    elif spec.deadline_at is not None:
        deadline_seconds = max(
            60,
            int((spec.deadline_at - _utcnow()).total_seconds()) + 30,
        )
    else:
        deadline_seconds = 3600
    # Boot beacon (written into the script body right after `mkdir -p`): a
    # provisional FAILED terminal record laid down BEFORE the clone/command so an
    # ungraceful VM death mid-run (OOM, host kill) still leaves a durable
    # terminal.json in the bundle. The reconcile path then reports
    # `vm_terminated_before_completion` instead of the blind "stopped without a
    # terminal marker" FAILURE that uploaded nothing. Every later phase
    # (clone-fail / runtime-TTL / completion) OVERWRITES this record with the true
    # outcome, so it only survives a death that writes nothing else.
    # Optional setup step: run AFTER the repo clone, BEFORE cwd + main command.
    # On failure, emit a typed serial reason and poweroff — no main command runs.
    # Setup stdout+stderr are captured to setup_step.log in the artifact dir so
    # they land in the durable GCS bundle for post-hoc diagnosis.
    setup_step_block = ""
    if spec.setup_step:
        _setup_log_q = shlex.quote(os.path.join(_GCP_ARTIFACT_DIR_PATH, "setup_step.log"))
        _setup_step_q = shlex.quote(spec.setup_step)
        setup_step_block = (
            f"if ! bash -lc {_setup_step_q} >{_setup_log_q} 2>&1; then\n"
            f'  echo "{_SERIAL_REASON_PREFIX}setup_step_failed"\n'
            f'  echo "{_SERIAL_STATUS_PREFIX}FAILED"\n'
            f'  echo "{_SERIAL_EXIT_PREFIX}1"\n'
            f"  python3 /tmp/blueprint_terminal_record.py "
            f"{artifact_dir} FAILED 1 setup_step_failed "
            f"{result_file} {command_log_path} >/dev/null 2>&1 || true\n"
            f"  upload_artifacts\n"
            f'  kill "$TTL_PID" "$_NP_WATCHDOG_PID" "$_VIEWABLE_PID" 2>/dev/null || true\n'
            f"  poweroff || shutdown -h now || true\n"
            f"  exit 1\n"
            f"fi"
        )
    # In container mode the command runner still runs `bash -lc <command>`, but
    # <command> is now the full `docker run … bash -lc <inner>` invocation, so the
    # log-capture / live-tail machinery is unchanged.
    docker_setup_block = ""
    if spec.container_image:
        command = shlex.quote(
            f"{container_run_prefix} bash -lc {shlex.quote(spec.command)}"
        )
        # `docker login` (only when a password is supplied) followed by
        # `docker pull`. Credentials are injected as shell env vars delivered via
        # the encrypted instance-metadata startup script and consumed through
        # --password-stdin — the password never appears on a command line, on the
        # serial console, or in any uploaded artifact/log. A failed pull terminates
        # LOUDLY with the parseable `container_image_pull_failed` reason on both the
        # serial marker and the durable terminal-record paths (mirroring the
        # clone-fail branch) rather than letting the command die with no marker.
        pull_log = shlex.quote(os.path.join(_GCP_ARTIFACT_DIR_PATH, "pull.log"))
        login_block = ""
        auth = spec.container_registry_auth or {}
        pwd = str(auth.get("password") or "")
        if pwd:
            login_block = (
                f"export BLUEPRINT_REGISTRY_SERVER="
                f"{shlex.quote(str(auth.get('server') or ''))}\n"
                f"export BLUEPRINT_REGISTRY_USERNAME="
                f"{shlex.quote(str(auth.get('username') or ''))}\n"
                f"export BLUEPRINT_REGISTRY_PASSWORD={shlex.quote(pwd)}\n"
                'if [ -n "${BLUEPRINT_REGISTRY_PASSWORD:-}" ]; then\n'
                '  printf %s "${BLUEPRINT_REGISTRY_PASSWORD}" | '
                'docker login "${BLUEPRINT_REGISTRY_SERVER}" '
                '-u "${BLUEPRINT_REGISTRY_USERNAME}" '
                "--password-stdin >/dev/null 2>&1 || true\n"
                "fi\n"
            )
        docker_setup_block = (
            login_block
            + 'echo "Pulling container image..."\n'
            + f"if ! docker pull {shlex.quote(spec.container_image)} "
            + f">{pull_log} 2>&1; then\n"
            + f'  echo "{_SERIAL_REASON_PREFIX}container_image_pull_failed"\n'
            + f'  echo "{_SERIAL_STATUS_PREFIX}FAILED"\n'
            + f'  echo "{_SERIAL_EXIT_PREFIX}1"\n'
            + "  python3 /tmp/blueprint_terminal_record.py "
            + f"{artifact_dir} FAILED 1 container_image_pull_failed "
            + f"{result_file} {command_log_path} >/dev/null 2>&1 || true\n"
            + "  upload_artifacts\n"
            + '  kill "$TTL_PID" "$_NP_WATCHDOG_PID" 2>/dev/null || true\n'
            + "  poweroff || shutdown -h now || true\n"
            + "  exit 1\n"
            + "fi\n"
        )
    else:
        command = shlex.quote(spec.command)
    artifact_bundle = shlex.quote(_GCP_ARTIFACT_BUNDLE_PATH)
    artifact_upload_url = shlex.quote(artifact_upload_url)
    upload_timeout = _GCP_ARTIFACT_UPLOAD_TIMEOUT_SECONDS
    # Cross-job checkpoint resume: pull the predecessor's durable bundle from its
    # signed GET URL and extract it into the artifact dir BEFORE the command runs,
    # so a resumed job sees the prior checkpoint already present. Kept to
    # single-line curl / python3 / tar invocations (no heredoc nesting) per the
    # repo's "generating code for one language from another" guidance. Fail-closed
    # by design, mirroring the in-process backend's `_rehydrate_input_artifact_bundle`
    # (which RAISES on a missing/unreadable predecessor bundle): a failed
    # download/extract writes a `resume_hydration.failed` marker and then ABORTS the
    # run — durable terminal record `resume_hydration_failed`, serial FAILED markers,
    # artifact upload, poweroff — rather than silently running from an empty
    # workspace and burning the whole deadline redoing (or misreporting) work the
    # resume was meant to skip. A success writes `resume_hydration.ok` and continues.
    # Both markers land in the uploaded bundle. Stale run-status files from the
    # predecessor (result.json /
    # command.log.json / terminal.json / live_tail.b64) are removed after extract
    # so they cannot contaminate THIS run's terminal detection; the boot beacon
    # below then re-lays a fresh terminal.json.
    resume_block = ""
    if input_artifact_download_url:
        _resume_url_q = shlex.quote(input_artifact_download_url)
        _resume_bundle_q = shlex.quote("/tmp/blueprint_resume_bundle.tar.gz")
        _resume_ok_q = shlex.quote(
            os.path.join(_GCP_ARTIFACT_DIR_PATH, "resume_hydration.ok")
        )
        _resume_fail_q = shlex.quote(
            os.path.join(_GCP_ARTIFACT_DIR_PATH, "resume_hydration.failed")
        )
        _resume_stage_q = shlex.quote(f"{_GCP_ARTIFACT_DIR_PATH}.resume-staging")
        _resume_stale = " ".join(
            shlex.quote(os.path.join(_GCP_ARTIFACT_DIR_PATH, _name))
            for _name in (
                "result.json",
                "command.log.json",
                "terminal.json",
                "live_tail.b64",
            )
        )
        resume_block = (
            f"rm -f {_resume_bundle_q}\n"
            f"rm -rf {_resume_stage_q} && mkdir -p {_resume_stage_q}\n"
            f"if timeout {upload_timeout} curl --fail --silent --show-error "
            f"--location --output {_resume_bundle_q} {_resume_url_q} "
            f">/dev/null 2>&1 || timeout {upload_timeout} python3 -c "
            "'import urllib.request,sys; "
            "urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "
            f"{_resume_url_q} {_resume_bundle_q} >/dev/null 2>&1; then\n"
            f"  RESUME_DL=1\n"
            f"else\n"
            f"  RESUME_DL=0\n"
            f"fi\n"
            f'if [ "$RESUME_DL" = 1 ] && [ -s {_resume_bundle_q} ] && '
            f"tar -C {_resume_stage_q} --no-same-owner -xzf {_resume_bundle_q} "
            f">/dev/null 2>&1; then\n"
            f"  rm -f {_resume_stale.replace(_GCP_ARTIFACT_DIR_PATH, _GCP_ARTIFACT_DIR_PATH + '.resume-staging')}\n"
            f"  _BP_RESUME_FC=$(find {_resume_stage_q} -type f 2>/dev/null | wc -l)\n"
            f"  if [ \"$_BP_RESUME_FC\" -lt 1 ]; then RESUME_DL=0; fi\n"
            f"  if [ \"$RESUME_DL\" = 1 ]; then rm -rf {artifact_dir} && mv {_resume_stage_q} {artifact_dir} || RESUME_DL=0; fi\n"
            f"  __BP_RESUME_D=$(python3 -c 'import json,base64,sys; print("
            f"base64.b64encode(json.dumps({{"
            f'"ok":True,"file_count":int(sys.argv[1]),"resumed_from_job_id":{json.dumps(spec.input_artifact_job_id)} }}).encode()).decode())\' '
            f"\"$_BP_RESUME_FC\" 2>/dev/null || true)\n"
            f'  [ "$RESUME_DL" = 1 ] && [ -n "$__BP_RESUME_D" ] && echo "{_SERIAL_RESUME_PREFIX}$__BP_RESUME_D" || true\n'
            f"  [ \"$RESUME_DL\" = 1 ] && echo resumed > {_resume_ok_q} 2>/dev/null || true\n"
            f'  [ "$RESUME_DL" = 1 ] && echo "[resume] hydrated predecessor checkpoint bundle into '
            f'artifact dir"\n'
            f"fi\n"
            f'if [ "$RESUME_DL" != 1 ]; then\n'
            f"  echo failed > {_resume_fail_q} 2>/dev/null || true\n"
            f'  echo "[resume] predecessor checkpoint hydration FAILED — aborting '
            f'run (fail-closed; refusing to run against an empty workspace)"\n'
            f'  echo "{_SERIAL_REASON_PREFIX}resume_hydration_failed"\n'
            f'  echo "{_SERIAL_STATUS_PREFIX}FAILED"\n'
            f'  echo "{_SERIAL_EXIT_PREFIX}1"\n'
            f'  echo "{_SERIAL_RESUME_PREFIX}{_SERIAL_RESUME_FAILED_B64}"\n'
            f"  python3 /tmp/blueprint_terminal_record.py "
            f"{artifact_dir} FAILED 1 resume_hydration_failed "
            f"{result_file} {command_log_path} >/dev/null 2>&1 || true\n"
            f"  upload_artifacts\n"
            f'  kill "$TTL_PID" "$_NP_WATCHDOG_PID" "$_VIEWABLE_PID" 2>/dev/null || true\n'
            f"  poweroff || shutdown -h now || true\n"
            f"  exit 1\n"
            f"fi\n"
            f"rm -f {_resume_bundle_q}"
        )
    max_log_bytes = _MAX_COMMAND_LOG_BYTES
    command_runner_b64 = _COMMAND_RUNNER_B64
    artifact_upload_b64 = _ARTIFACT_UPLOAD_B64
    terminal_record_b64 = _TERMINAL_RECORD_B64
    heartbeat_body_b64 = _HEARTBEAT_BODY_B64
    orchestrator_url_q = shlex.quote(compute.ORCHESTRATOR_URL)
    job_id_q = shlex.quote(str(spec.job_id))
    # Per-job heartbeat bearer secret (NOT the raw job UUID). Embedded into the
    # startup script — which is delivered as encrypted instance metadata and never
    # echoed by any API — so a mere UUID observer cannot forge a heartbeat and
    # quiet the no-progress watchdog or keep a stuck STARTING job alive.
    hb_token_q = shlex.quote(compute_job_heartbeat_secret(spec.job_id))
    np_timeout = (
        max(60, int(spec.no_progress_timeout_seconds))
        if spec.no_progress_timeout_seconds is not None
        and spec.no_progress_timeout_seconds > 0
        else 0
    )
    return f"""#!/bin/bash
set -u -o pipefail
TTL_SECONDS={deadline_seconds}
_NP_TIMEOUT_SECONDS={np_timeout}
echo '{command_runner_b64}' | base64 -d > /tmp/blueprint_command_runner.py
echo '{artifact_upload_b64}' | base64 -d > /tmp/blueprint_artifact_upload.py
echo '{terminal_record_b64}' | base64 -d > /tmp/blueprint_terminal_record.py
ARTIFACTS_UPLOADED=0
upload_artifacts() {{
  if [ "$ARTIFACTS_UPLOADED" -ne 0 ]; then
    return 0
  fi
  if [ ! -d {artifact_dir} ] || [ -z "$(ls -A {artifact_dir} 2>/dev/null)" ]; then
    return 0
  fi
  rm -f {artifact_bundle}
  if tar -C {artifact_dir} -czf {artifact_bundle} . >/dev/null 2>&1; then
    if [ -s {artifact_bundle} ]; then
      if command -v curl >/dev/null 2>&1; then
        if timeout {upload_timeout} curl --fail --silent --show-error \
          --request PUT \
          --upload-file {artifact_bundle} \
          --header "Content-Type: application/gzip" \
          {artifact_upload_url} >/dev/null 2>&1; then
          ARTIFACTS_UPLOADED=1
        fi
      elif command -v python3 >/dev/null 2>&1; then
        if timeout {upload_timeout} python3 /tmp/blueprint_artifact_upload.py \
          {artifact_upload_url} {artifact_bundle} >/dev/null 2>&1; then
          ARTIFACTS_UPLOADED=1
        fi
      fi
    fi
  fi
  rm -f {artifact_bundle}
  return 0
}}
trap upload_artifacts EXIT HUP INT TERM QUIT
(
  sleep "$TTL_SECONDS"
  python3 /tmp/blueprint_terminal_record.py \
    {artifact_dir} FAILED 124 runtime_ttl {result_file} {command_log_path} \
    >/dev/null 2>&1 || true
  echo "{_SERIAL_REASON_PREFIX}runtime_ttl"
  echo "{_SERIAL_STATUS_PREFIX}FAILED"
  echo "{_SERIAL_EXIT_PREFIX}124"
  upload_artifacts
  poweroff || shutdown -h now || true
) &
TTL_PID=$!
# The no-progress watchdog is ARMED LATER (right before the command runs), after
# setup — repo clone, container image pull, resume hydration — completes, so slow
# provisioning is never mistaken for a stalled job. Declared empty here so the
# `set -u` script can reference "$_NP_WATCHDOG_PID" in the setup-failure kill
# paths (clone/setup/pull) that run before the watchdog is armed.
_NP_WATCHDOG_PID=
_VIEWABLE_PID=
echo '{heartbeat_body_b64}' | base64 -d > /tmp/blueprint_heartbeat_body.py
_COMPUTE_ORCHESTRATOR_URL={orchestrator_url_q}
_COMPUTE_JOB_ID={job_id_q}
_COMPUTE_HB_TOKEN={hb_token_q}
_compute_heartbeat() {{
  # Build the heartbeat body off-VM in a real, tested Python file (base64-baked
  # above): it merges the live-output tail with the latest {{"metric","value"}}
  # record from the progress file into {{"live_output_tail":..,"progress":..}}.
  # The progress sample is what feeds the server's no-progress watchdog, so a job
  # that keeps writing its declared metric stays distinguishable from a dead one.
  _HB_BODY=$(python3 /tmp/blueprint_heartbeat_body.py {live_tail_path} {progress_file} 2>/dev/null || true)
  if [ -z "$_HB_BODY" ]; then
    _HB_BODY='{{}}'
  fi
  curl -sf --max-time 5 -X POST -H "Authorization: Bearer $_COMPUTE_HB_TOKEN" -H "Content-Type: application/json" -d "$_HB_BODY" "$_COMPUTE_ORCHESTRATOR_URL/api/agent/compute-jobs/$_COMPUTE_JOB_ID/heartbeat" >/dev/null 2>&1 || true
}}
_compute_heartbeat
(
  while true; do
    sleep 55
    _compute_heartbeat
  done
) &
_HEARTBEAT_PID=$!
echo '{_VIEWABLE_WATCH_B64}' | base64 -d > /tmp/blueprint_viewable_watch.py
python3 /tmp/blueprint_viewable_watch.py \
  {artifact_dir} \
  "$_COMPUTE_ORCHESTRATOR_URL" \
  "$_COMPUTE_JOB_ID" \
  "$_COMPUTE_HB_TOKEN" \
  >/dev/null 2>&1 &
_VIEWABLE_PID=$!
echo "{_SERIAL_STATUS_PREFIX}RUNNING"
{env_block}
export {RESULT_FILE_ENV}={result_file}
export {ARTIFACT_DIR_ENV}={artifact_dir}
export {PROGRESS_FILE_ENV}={progress_file}
rm -f {result_file}
rm -f {command_log_path}
mkdir -p {artifact_dir}
{resume_block}
python3 /tmp/blueprint_terminal_record.py \
  {artifact_dir} FAILED 1 vm_terminated_before_completion {result_file} {command_log_path} \
  >/dev/null 2>&1 || true
{repo_setup_block}
{setup_step_block}
{cwd_line}
{docker_setup_block}# No-progress watchdog: ARMED HERE, after all setup (repo clone, container image
# pull, resume hydration, setup step) has completed, so slow provisioning never
# counts as "no progress" and cannot trip a false kill. Mirrors the local
# subprocess wrapper, which launches its watchdog after setup_step + container
# pull. Polls the progress file's mtime; each interval that advances it resets
# the deadline (sliding window). When _NP_TIMEOUT_SECONDS is 0 the block is inert.
touch {progress_file} 2>/dev/null || true
(
  if [ "$_NP_TIMEOUT_SECONDS" -gt 0 ]; then
    _NP_START=$(stat -c %Y {progress_file} 2>/dev/null || echo 0)
    while true; do
      sleep "$_NP_TIMEOUT_SECONDS"
      _NP_NOW=$(stat -c %Y {progress_file} 2>/dev/null || echo 0)
      if [ "$_NP_NOW" = "$_NP_START" ]; then
        python3 /tmp/blueprint_terminal_record.py \
          {artifact_dir} FAILED 125 no_progress_timeout {result_file} {command_log_path} \
          >/dev/null 2>&1 || true
        echo "{_SERIAL_REASON_PREFIX}no_progress_timeout"
        echo "{_SERIAL_STATUS_PREFIX}FAILED"
        echo "{_SERIAL_EXIT_PREFIX}125"
        upload_artifacts
        poweroff || shutdown -h now || true
        exit 0
      fi
      _NP_START=$_NP_NOW
    done
  fi
) &
_NP_WATCHDOG_PID=$!
EXIT_CODE=0
if python3 /tmp/blueprint_command_runner.py \
  {command} \
  {command_log_path} \
  {max_log_bytes} \
  {live_tail_path}; then
  EXIT_CODE=0
  STATUS=SUCCEEDED
  REASON=completed
else
  EXIT_CODE=$?
  STATUS=FAILED
  REASON=command_failed
fi
echo "{_SERIAL_REASON_PREFIX}$REASON"
echo "{_SERIAL_STATUS_PREFIX}$STATUS"
echo "{_SERIAL_EXIT_PREFIX}$EXIT_CODE"
if [ -f {result_file} ]; then
  METRICS_B64=$(base64 -w0 < {result_file} 2>/dev/null || true)
  if [ -n "$METRICS_B64" ]; then
    echo "{_SERIAL_METRICS_PREFIX}$METRICS_B64"
  fi
fi
COMMAND_LOG_B64=""
if [ -s {command_log_path} ]; then
  COMMAND_LOG_B64=$(base64 -w0 < {command_log_path} 2>/dev/null || true)
  if [ -n "$COMMAND_LOG_B64" ]; then
    echo "{_SERIAL_COMMAND_LOG_PREFIX}$COMMAND_LOG_B64"
  fi
fi
if [ -n "${{BLUEPRINT_RELEASE_SHA:-}}" ]; then
  RELEASE_ECHO_B64=$(printf '%s' "${{BLUEPRINT_RELEASE_SHA}}" | base64 -w0 2>/dev/null || true)
  if [ -n "$RELEASE_ECHO_B64" ]; then
    echo "{_SERIAL_RELEASE_ECHO_PREFIX}$RELEASE_ECHO_B64"
  fi
fi
python3 /tmp/blueprint_terminal_record.py \
  {artifact_dir} "$STATUS" "$EXIT_CODE" "$REASON" {result_file} {command_log_path} \
  >/dev/null 2>&1 || true
upload_artifacts
kill "$TTL_PID" "$_HEARTBEAT_PID" "$_NP_WATCHDOG_PID" "$_VIEWABLE_PID" 2>/dev/null || true
poweroff || shutdown -h now || true
exit "$EXIT_CODE"
"""


def _decode_result_metrics(raw_b64: str | None) -> dict[str, Any] | None:
    """Decode a base64 metrics marker into a bounded dict.

    Defensive by contract: any decode/parse/shape error — missing, oversized,
    non-base64, non-JSON, non-object — yields ``None`` (metrics simply ABSENT).
    Per-entry, only numeric and boolean values survive (bool coerced to a
    number); strings, nulls, and other nested objects/arrays are dropped.
    Exception: the ``arms`` key (an A/B multi-arm comparison block — a dict or
    list) is preserved verbatim so downstream consumers can render per-arm
    results. The monitor must NEVER raise on a garbled marker, so all failure
    modes funnel to ``None``.
    """
    if not raw_b64:
        return None
    candidate = raw_b64.strip()
    if not candidate:
        return None
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) > _MAX_RESULT_METRICS_DECODED_BYTES:
        return None
    try:
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    metrics: dict[str, Any] = {}
    for key, value in parsed.items():
        if len(metrics) >= _MAX_RESULT_METRICS:
            break
        if not isinstance(key, str):
            continue
        # Preserve the arms block verbatim — it is a structured A/B comparison
        # block (dict or non-empty list) and must survive into polling_metadata
        # so _extract_experiment_arms can find and surface it.
        if key == "arms" and isinstance(value, (dict, list)) and value:
            metrics[key] = value
            continue
        # bool is a subclass of int — coerce explicitly before the int/float test.
        if isinstance(value, bool):
            metrics[key] = float(value)
        elif isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                continue
            metrics[key] = numeric
        # strings, None, other nested dict/list → dropped (not numeric)
    return metrics or None


def _decode_command_log(raw_b64: str | None) -> dict[str, Any] | None:
    """Decode a bounded command-log marker into a structured payload."""
    if not raw_b64:
        return None
    candidate = raw_b64.strip()
    if not candidate:
        return None
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) > _MAX_COMMAND_LOG_DECODED_BYTES:
        return None
    try:
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    text = parsed.get("text")
    truncated = parsed.get("truncated")
    byte_count = parsed.get("byte_count")
    if not isinstance(text, str):
        return None
    if not isinstance(truncated, bool):
        return None
    if not isinstance(byte_count, int) or byte_count < 0:
        return None
    return {
        "text": text,
        "truncated": truncated,
        "byte_count": byte_count,
    }


def _decode_release_echo_sha(raw_b64: str | None) -> str | None:
    """Decode a base64 release-echo SHA marker. Returns None on any error."""
    if not raw_b64:
        return None
    candidate = raw_b64.strip()
    if not candidate:
        return None
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        result = decoded.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return result or None


def _decode_resume_provenance(raw_b64: str | None) -> dict[str, Any] | None:
    """Decode a base64 JSON resume-provenance marker. Returns None on any error."""
    if not raw_b64:
        return None
    candidate = raw_b64.strip()
    if not candidate:
        return None
    try:
        decoded = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        raw = decoded.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data:
        return data
    return None


def _parse_serial_status(serial_output: str) -> ComputeStatusResult | None:
    status_line: str | None = None
    exit_code: int | None = None
    reason: str | None = None
    metrics_b64: str | None = None
    command_log_b64: str | None = None
    release_echo_b64: str | None = None
    resume_provenance_b64: str | None = None
    for raw_line in serial_output.splitlines():
        line = raw_line.strip()
        if line.startswith(_SERIAL_STATUS_PREFIX):
            status_line = line[len(_SERIAL_STATUS_PREFIX) :]
        elif line.startswith(_SERIAL_EXIT_PREFIX):
            try:
                exit_code = int(line[len(_SERIAL_EXIT_PREFIX) :])
            except ValueError:
                exit_code = None
        elif line.startswith(_SERIAL_REASON_PREFIX):
            reason = line[len(_SERIAL_REASON_PREFIX) :]
        elif line.startswith(_SERIAL_METRICS_PREFIX):
            metrics_b64 = line[len(_SERIAL_METRICS_PREFIX) :]
        elif line.startswith(_SERIAL_COMMAND_LOG_PREFIX):
            command_log_b64 = line[len(_SERIAL_COMMAND_LOG_PREFIX) :]
        elif line.startswith(_SERIAL_RELEASE_ECHO_PREFIX):
            release_echo_b64 = line[len(_SERIAL_RELEASE_ECHO_PREFIX) :]
        elif line.startswith(_SERIAL_RESUME_PREFIX):
            resume_provenance_b64 = line[len(_SERIAL_RESUME_PREFIX) :]
    if status_line is None:
        return None
    if status_line == "RUNNING":
        return ComputeStatusResult(
            status=ComputeJobStatus.RUNNING,
            resume_provenance=_decode_resume_provenance(resume_provenance_b64),
        )
    metrics = _decode_result_metrics(metrics_b64)
    command_log = _decode_command_log(command_log_b64)
    echoed_release_sha = _decode_release_echo_sha(release_echo_b64)
    resume_provenance = _decode_resume_provenance(resume_provenance_b64)
    if status_line == "SUCCEEDED":
        return ComputeStatusResult(
            status=ComputeJobStatus.SUCCEEDED,
            message=reason,
            exit_code=exit_code,
            finished_at=_utcnow(),
            metrics=metrics,
            command_log=command_log,
            echoed_release_sha=echoed_release_sha,
            resume_provenance=resume_provenance,
        )
    return ComputeStatusResult(
        status=ComputeJobStatus.FAILED,
        message=reason or "compute-job startup script failed",
        exit_code=exit_code,
        finished_at=_utcnow(),
        metrics=metrics,
        command_log=command_log,
        echoed_release_sha=echoed_release_sha,
        resume_provenance=resume_provenance,
    )


def _extract_terminal_record_from_bundle(raw: bytes) -> dict[str, Any] | None:
    """Pull ``terminal.json`` out of a durable ``.tar.gz`` artifact bundle.

    The bundle is produced by ``tar -C <artifact_dir> -czf - .`` so members are
    named ``./terminal.json`` etc.; we match on the basename and read ONLY that
    one member (never extracting to disk, so a crafted member path cannot
    traverse). Defensive by contract: a missing member, an oversized record, a
    non-gzip/non-tar blob, or any parse error yields ``None`` — reconciliation
    treats that identically to 'no durable record'.
    """
    if not raw:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            member = None
            for candidate in tar:
                if not candidate.isfile():
                    continue
                if os.path.basename(candidate.name) == _GCP_TERMINAL_RECORD_FILENAME:
                    member = candidate
                    break
            if member is None:
                return None
            if member.size > _MAX_TERMINAL_RECORD_DECODED_BYTES:
                return None
            extracted = tar.extractfile(member)
            if extracted is None:
                return None
            data = extracted.read(_MAX_TERMINAL_RECORD_DECODED_BYTES + 1)
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return None
    if len(data) > _MAX_TERMINAL_RECORD_DECODED_BYTES:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _reconcile_terminal_record_to_status(
    record: dict[str, Any],
) -> ComputeStatusResult | None:
    """Convert a durable ``terminal.json`` record into a ``ComputeStatusResult``.

    Mirrors the SUCCEEDED/FAILED branches of :func:`_parse_serial_status` exactly
    — same base64 decoders for metrics / command-log / release-echo — so a job
    reconciled from its durable record is indistinguishable from one finalized
    via the serial fast-path. Returns ``None`` when ``status`` is absent or not a
    recognized terminal value (the record is then treated as no durable record).
    """
    raw_status = record.get("status")
    if raw_status not in {"SUCCEEDED", "FAILED"}:
        return None
    raw_exit = record.get("exit_code")
    # bool is an int subclass — exclude it so a stray ``true`` isn't read as 1.
    exit_code = (
        raw_exit
        if isinstance(raw_exit, int) and not isinstance(raw_exit, bool)
        else None
    )
    reason = record.get("reason")
    if not isinstance(reason, str):
        reason = None
    metrics = _decode_result_metrics(record.get("metrics_b64"))
    command_log = _decode_command_log(record.get("command_log_b64"))
    echoed_release_sha = _decode_release_echo_sha(record.get("release_echo_b64"))
    if raw_status == "SUCCEEDED":
        return ComputeStatusResult(
            status=ComputeJobStatus.SUCCEEDED,
            message=reason,
            exit_code=0 if exit_code is None else exit_code,
            finished_at=_utcnow(),
            metrics=metrics,
            command_log=command_log,
            echoed_release_sha=echoed_release_sha,
        )
    return ComputeStatusResult(
        status=ComputeJobStatus.FAILED,
        message=reason or "compute-job startup script failed",
        exit_code=exit_code,
        finished_at=_utcnow(),
        metrics=metrics,
        command_log=command_log,
        echoed_release_sha=echoed_release_sha,
    )


async def _reconcile_from_durable_terminal_record(
    job: ComputeJob,
) -> ComputeStatusResult | None:
    """Recover a terminal GCP job's outcome from its durable GCS terminal record.

    A fast job can power off in the gap between emitting its serial
    ``BLUEPRINT_COMPUTE_STATUS=`` marker and the orchestrator's next serial poll
    — and GCP serial output goes empty once the VM is TERMINATED — so a
    successful run is otherwise misclassified FAILURE. The authoritative outcome
    still lives in ``terminal.json`` inside the durable artifact bundle: download
    the bundle, extract the record, and reconcile to the real SUCCEEDED/FAILED
    result. Returns ``None`` when no durable terminal record exists (the caller
    then falls back to the blind 'stopped without a terminal marker' FAILURE).
    """
    raw = await storage.download_compute_job_artifact_bundle(job.id)
    if not raw:
        return None
    record = _extract_terminal_record_from_bundle(raw)
    if not record:
        return None
    return _reconcile_terminal_record_to_status(record)


# ---------------------------------------------------------------------------
# Thread-pool helpers for LocalComputeBackend (avoids blocking the event loop
# on sync file I/O and subprocess.Popen).
# ---------------------------------------------------------------------------


def _launch_setup_state_dir(external_id: str, started_at_iso: str) -> None:
    """Bundle os.makedirs + _write_local_state_text + os.remove into one
    thread-pool call so the event loop is not blocked three times."""
    os.makedirs(_local_state_dir_for_external_id(external_id), exist_ok=True)
    _write_local_state_text(
        _local_started_at_path_for_external_id(external_id),
        started_at_iso,
    )
    _remove_local_terminal_status_for_external_id(external_id)


def _launch_spawn_subprocess(
    cmd: list[str],
    cwd: str | None,
    env: dict[str, str],
) -> subprocess.Popen:
    """Spawn a subprocess inside a thread-pool thread so Popen's fork+exec
    (and any preexec_fn/env setup overhead) does not block the event loop."""
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ── Per-project compute image build (PRODUCE side of images-first) ───────────
#
# Bakes a digest-pinned container image from a project's own lockfiles. The
# reusable, stdlib-only logic lives in :mod:`app.compute_image_build` (also the
# home the standalone ``scripts/build_compute_image.py`` CLI loads by path). The
# backend reads the project's manifest ``compute.image.build`` section, checks
# out the repo, and drives the hash-gated build.

from app import compute_image_build  # noqa: E402

# Files, in priority order, that count as dependency inputs when the manifest
# does not enumerate them. Any that exist in the checkout are hashed.
_DEFAULT_DEP_FILES = (
    "requirements.txt",
    "requirements.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "pyproject.toml",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "environment.yml",
    "conda-lock.yml",
)


def resolve_project_compute_image_build(
    manifest_compute: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract the buildable ``compute.image`` config from a manifest result.

    *manifest_compute* is the ``compute`` dict returned by
    ``admin_project_tools._discover_manifest_tools`` (shape
    ``{"image": {"name": ..., "build": {...} | None}}``). Returns a dict
    ``{"name", "dockerfile", "parent", "build_args"}`` when the project declares
    a ``build`` section, or ``None`` when it only points at a prebuilt image
    (or declares no compute image at all).
    """
    if not isinstance(manifest_compute, dict):
        return None
    image = manifest_compute.get("image")
    if not isinstance(image, dict):
        return None
    build = image.get("build")
    if not isinstance(build, dict):
        return None
    name = image.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return {
        "name": name.strip(),
        "dockerfile": build.get("dockerfile") or ".blueprint/Dockerfile.compute",
        "parent": build.get("parent"),
        "build_args": dict(build.get("build_args") or {}),
    }


def _discover_dep_files(repo_dir: str) -> list[str]:
    """Return the default dependency files that actually exist in *repo_dir*."""
    present: list[str] = []
    for rel in _DEFAULT_DEP_FILES:
        if os.path.isfile(os.path.join(repo_dir, rel)):
            present.append(rel)
    return present


def _git_clone_ref(clone_url: str, ref: str, dest: str) -> None:
    """Shallow-clone *clone_url* at *ref* into *dest*. Raises on failure."""
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, clone_url, dest],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        # --branch fails for a raw SHA; fall back to full clone + checkout.
        proc2 = subprocess.run(
            ["git", "clone", clone_url, dest],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proc2.returncode != 0:
            raw = (proc.stderr or proc2.stderr or "")[-1000:]
            # Redact any embedded credentials from the URL before surfacing in logs.
            safe = re.sub(r"//[^@\s]+@", "//***@", raw)
            raise RuntimeError(f"git clone failed: {safe}")
        co = subprocess.run(
            ["git", "-C", dest, "checkout", ref],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if co.returncode != 0:
            raise RuntimeError(f"git checkout {ref} failed: {(co.stderr or '')[-500:]}")


async def build_project_compute_image(
    project: "Project",
    *,
    registry_repo: str,
    build_config: dict[str, Any],
    ref: str | None = None,
    dep_files: Sequence[str] | None = None,
    dry_run: bool = False,
    _token_fn=None,
    _clone_fn=None,
    _build_fn=None,
) -> Any:
    """Bake (or reuse) the project's compute image, returning a ``BuildResult``.

    Clones the project repo at *ref* into a temp dir, then delegates to
    ``scripts/build_compute_image.build_and_push`` which content-hashes the
    dependency inputs, no-ops if an image already exists for that hash, scans
    for platform credentials, and otherwise builds + pushes a digest-pinned
    image. Returns the build script's ``BuildResult`` (carries ``pinned_ref``).

    *build_config* is the dict from :func:`resolve_project_compute_image_build`.
    Hooks (``_token_fn``/``_clone_fn``/``_build_fn``) exist for tests; production
    resolves an installation token and uses git + the real build script.
    """
    import tempfile as _tempfile

    build_fn = _build_fn or compute_image_build.build_and_push

    owner = (getattr(project, "github_owner", None) or "").strip()
    repo = (getattr(project, "github_repo", None) or "").strip()
    if not owner or not repo:
        raise ValueError("project has no connected GitHub repo — cannot build image")

    resolved_ref = (
        ref or getattr(project, "promotion_target", None) or "main"
    )
    image_name = str(build_config["name"]).rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]

    with _tempfile.TemporaryDirectory(prefix="compute-image-build-") as tmp:
        repo_dir = os.path.join(tmp, "repo")

        # Resolve a short-lived read token and clone (unless a hook overrides).
        if _clone_fn is not None:
            await asyncio.to_thread(_clone_fn, owner, repo, resolved_ref, repo_dir)
        else:
            token = None
            if _token_fn is not None:
                token = await _token_fn(project)
            else:
                try:
                    from app.branch_cleanup import _get_project_github_token
                    from app.database import AsyncSessionLocal

                    async with AsyncSessionLocal() as session:
                        db_project = await session.get(Project, project.id)
                        token = await _get_project_github_token(
                            db_project or project, session
                        )
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(f"could not mint repo token: {e}") from e
            if not token:
                raise RuntimeError("no GitHub token available for repo clone")
            clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
            await asyncio.to_thread(_git_clone_ref, clone_url, resolved_ref, repo_dir)

        resolved_dep_files = list(dep_files) if dep_files is not None else _discover_dep_files(repo_dir)

        result = await asyncio.to_thread(
            build_fn,
            repo_dir=repo_dir,
            registry_repo=registry_repo,
            image_name=image_name,
            dockerfile=build_config.get("dockerfile") or ".blueprint/Dockerfile.compute",
            dep_files=resolved_dep_files,
            parent=build_config.get("parent"),
            build_args=dict(build_config.get("build_args") or {}),
            dry_run=dry_run,
        )
    return result


def _parse_digest_from_pinned_ref(pinned_ref: str | None) -> str | None:
    """Extract the ``sha256:...`` digest from a pinned reference.

    A digest-pinned ref looks like ``registry/repo@sha256:<hex>``; the digest is
    the substring after the last ``@``. A tag-only ref (``registry/repo:tag``)
    has no ``@`` and yields ``None``.
    """
    if not pinned_ref or "@" not in pinned_ref:
        return None
    return pinned_ref.rsplit("@", 1)[1] or None


def _split_image_ref(ref: str) -> tuple[str, str, str]:
    """Split an image reference into ``(registry_repo, image_name, tag)``.

    ``us-docker.pkg.dev/proj/compute-images/my-project:v2``
        -> ``("us-docker.pkg.dev/proj/compute-images", "my-project", "v2")``

    The tag is split off the FINAL path segment only, so a registry host that
    carries a ``:port`` (``localhost:5000/my-project:v2``) keeps its port and
    still yields ``tag="v2"``. A ref with no registry prefix yields an empty
    registry; a ref with no tag yields an empty tag. Any ``@digest`` suffix is
    stripped first — callers handle already-pinned refs before this point, but
    the parse stays total rather than depending on that.

    Mirrors how :func:`build_project_compute_image` derives ``image_name`` and
    how :func:`resolve_compute_image_registry_repo` derives the registry, so a
    manifest ref decomposes into exactly the fields persisted on
    :class:`ProjectComputeImage`.
    """
    if not ref:
        return "", "", ""
    base = ref.split("@", 1)[0]
    head, sep, last = base.rpartition("/")
    registry_repo = head if sep else ""
    name, _, tag = last.partition(":")
    return registry_repo, name, tag


async def record_project_compute_image_build(
    session: Any,
    project_id: uuid.UUID,
    registry_repo: str,
    image_name: str,
    result: Any,
) -> ProjectComputeImage:
    """UPSERT a :class:`ProjectComputeImage` row from a ``BuildResult``.

    Keyed on ``(project_id, dep_hash)``: if a row already exists for that
    dependency hash it is updated in place (``pinned_ref``, ``digest``, ``tag``,
    ``built``, ``updated_at`` refreshed); otherwise a new row is inserted. The
    ``sha256:...`` digest is parsed out of ``result.pinned_ref`` (the substring
    after ``@``) and stored in ``digest`` — ``None`` when the ref is tag-pinned.

    A REAL upsert — ``INSERT … ON CONFLICT (project_id, dep_hash) DO UPDATE``,
    not a select-then-insert. Two concurrent build triggers for the same
    ``(project_id, dep_hash)`` (the common case: the same dependency set built
    from two clicks, or a click racing a CI trigger) both used to miss the
    existence check and race to INSERT; the loser died on
    ``uq_projectcomputeimage_project_dep_hash`` and surfaced as a 500. The
    conflict is now resolved by the database, so the second writer updates the
    row the first one inserted.

    Callers are responsible for committing the session.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as _pg_insert

    pinned_ref = getattr(result, "pinned_ref", None) or ""
    tag = getattr(result, "tag", None) or ""
    dep_hash = getattr(result, "dep_hash", None) or ""
    built = bool(getattr(result, "built", False))
    digest = _parse_digest_from_pinned_ref(pinned_ref)
    now = _utcnow()

    values = {
        "id": uuid.uuid4(),
        "project_id": project_id,
        "registry_repo": registry_repo,
        "image_name": image_name,
        "dep_hash": dep_hash,
        "tag": tag,
        "pinned_ref": pinned_ref,
        "digest": digest,
        "built": built,
        "created_at": now,
        "updated_at": now,
    }

    stmt = _pg_insert(ProjectComputeImage).values(**values)
    stmt = stmt.on_conflict_do_update(
        # The unique constraint the concurrent INSERT used to die on.
        index_elements=[
            ProjectComputeImage.project_id,
            ProjectComputeImage.dep_hash,
        ],
        set_={
            "registry_repo": stmt.excluded.registry_repo,
            "image_name": stmt.excluded.image_name,
            "tag": stmt.excluded.tag,
            "pinned_ref": stmt.excluded.pinned_ref,
            "digest": stmt.excluded.digest,
            "built": stmt.excluded.built,
            "updated_at": stmt.excluded.updated_at,
        },
    ).returning(ProjectComputeImage.id)

    row_id = (await session.execute(stmt)).scalar_one()

    # Re-read through the ORM so the caller gets a managed instance with the
    # post-upsert values (the same contract the select-then-insert had).
    row = (
        await session.execute(
            select(ProjectComputeImage)
            .where(ProjectComputeImage.id == row_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def _preserve_existing_digest_pin_cb(
    session: Any, project_id: uuid.UUID, result: Any
) -> Any:
    """Never let a digest-less build result overwrite a stored digest.

    The backend twin of ``admin_project_tools._preserve_existing_digest_pin``:
    an unchanged-deps re-trigger takes ``build_and_push``'s already-present fast
    path, which resolves the digest from the registry — but a briefly-unreachable
    registry (or a tag-only push) still yields ``digest=None``. Recording that
    would silently unpin the project's compute image. If a row for this
    ``(project_id, dep_hash)`` already carries a digest, keep it.

    Lives here (not imported from the router) so the reusable driver never has to
    reach up into a FastAPI router module — that would be an import cycle
    (``admin_project_tools`` imports ``compute_backends`` as ``_cb``).
    """
    from sqlalchemy import select

    pinned_ref = getattr(result, "pinned_ref", None) or ""
    if _parse_digest_from_pinned_ref(pinned_ref):
        return result  # carries a digest — nothing to protect

    dep_hash = getattr(result, "dep_hash", None) or ""
    if not dep_hash:
        return result

    existing = (
        await session.execute(
            select(ProjectComputeImage)
            .where(
                ProjectComputeImage.project_id == project_id,
                ProjectComputeImage.dep_hash == dep_hash,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None or not existing.digest:
        return result  # nothing stored to protect

    logger.warning(
        "[COMPUTE-IMAGE] auto-refresh for project=%s dep_hash=%s resolved no "
        "digest; keeping the stored pin %s rather than overwriting it with NULL",
        project_id,
        dep_hash,
        existing.pinned_ref,
    )
    return compute_image_build.BuildResult(
        pinned_ref=existing.pinned_ref,
        tag=existing.tag or getattr(result, "tag", "") or "",
        dep_hash=dep_hash,
        already_present=bool(getattr(result, "already_present", True)),
        built=bool(existing.built or getattr(result, "built", False)),
        digest=existing.digest,
    )


async def _record_unchanged_pin(
    session: Any,
    project_id: uuid.UUID,
    registry_repo: str,
    image_name: str,
    full_repo: str,
    dep_hash: str,
    resolve_digest_fn,
    record_fn,
) -> bool:
    """Best-effort: record the pin for an image that is ALREADY present.

    The fast-path no-op case: the registry already has this ``dep_hash`` tag, so
    there is nothing to build. We still want the durable ``ProjectComputeImage``
    pin to exist — but only when we can resolve a REAL ``sha256:`` digest.

    A ``None`` digest means UNKNOWN (registry briefly unreachable), not "no
    digest"; writing it would clobber a stored pin with NULL, so we refuse. This
    is the ``_preserve_existing_digest_pin`` philosophy applied to the fast path:
    only ever write a non-null digest. Returns whether a row was written.
    """
    try:
        digest = await asyncio.to_thread(resolve_digest_fn, full_repo, dep_hash)
    except Exception:  # noqa: BLE001 — registry helpers fail open
        digest = None
    if not digest:
        return False
    result = compute_image_build.BuildResult(
        pinned_ref=f"{full_repo}@{digest}",
        tag=dep_hash,
        dep_hash=dep_hash,
        already_present=True,
        # The image is present in the registry; the pin describes a real image.
        built=True,
        digest=digest,
    )
    await record_fn(session, project_id, registry_repo, image_name, result)
    await session.commit()
    return True


async def refresh_project_compute_image(
    session: Any,
    project: "Project",
    *,
    allow_build: bool = True,
    build_semaphore: "asyncio.Semaphore | None" = None,
    acquire_timeout: float = 1.0,
    progress_cb: "Callable[[], None] | None" = None,
    _manifest_fn=None,
    _token_fn=None,
    _default_branch_fn=None,
    _fetch_bytes_fn=None,
    _tag_exists_fn=None,
    _resolve_digest_fn=None,
    _build_fn=None,
    _record_fn=None,
) -> dict[str, Any]:
    """Auto-rebuild ONE project's baked compute image when its manifest changed.

    Closes the drift gap: today a project-baked compute image only ever rebuilds
    via the manual OWNER-only ``POST /projects/{id}/compute-image/build``. This
    driver lets a periodic sweep self-heal — a changed dependency manifest (any
    hashed dep file, or the compute Dockerfile) triggers an automatic rebuild +
    re-pin, while an unchanged manifest resolves to a cheap no-op with NO git
    clone and NO docker build.

    Steps, all best-effort — this function never raises into its caller:

    * **resolve config** — discover ``.blueprint/tools.json`` (cached) and extract
      ``compute.image.build`` via :func:`resolve_project_compute_image_build`;
      skip cleanly (``no_config``) with no connected repo, no read token, no build
      section, or no Dockerfile at the declared path.
    * **cheap current hash** — fetch the Dockerfile and each present
      :data:`_DEFAULT_DEP_FILES` from the repo's default branch via the GitHub
      file-fetch helpers (NO clone), keyed exactly the way ``build_and_push``
      assembles its inputs (Dockerfile as ``"Dockerfile"``, each dep file under
      its relative path) so :func:`compute_image_build.compute_dependency_hash`
      matches what a real build would produce.
    * **fast-path no-op** — if ``registry_tag_exists(full_repo, dep_hash)`` the
      image is current: do NOT build; best-effort record the pin (never overwrite
      an existing digest with NULL).
    * **rebuild** — only when the tag is ABSENT, drive the existing pipeline
      (:func:`build_project_compute_image` -> :func:`record_project_compute_image_build`),
      honoring the process-wide build concurrency guard (*build_semaphore*) and a
      caller-supplied build budget (*allow_build*). When *progress_cb* is provided,
      the build runs as a background task with periodic heartbeats so the reaper's
      lifecycle watchdog does not cancel the cycle during a multi-minute build.

    Returns a small structured result whose ``status`` is one of ``no_config`` /
    ``unchanged`` / ``rebuilt`` / ``deferred`` / ``error``. All hooks default to
    the real production functions; tests inject them so nothing hits docker or
    GitHub.
    """
    manifest_fn = _manifest_fn
    if manifest_fn is None:
        from app.routers.admin_project_tools import _discover_manifest_tools

        manifest_fn = _discover_manifest_tools
    if _token_fn is None:
        from app.github_auth import get_repo_installation_read_token

        token_fn = get_repo_installation_read_token
    else:
        token_fn = _token_fn
    if _default_branch_fn is None:
        from app.github_refs import get_repo_default_branch

        default_branch_fn = get_repo_default_branch
    else:
        default_branch_fn = _default_branch_fn
    if _fetch_bytes_fn is None:
        from app.github import get_file_bytes_at_ref

        fetch_bytes_fn = get_file_bytes_at_ref
    else:
        fetch_bytes_fn = _fetch_bytes_fn
    tag_exists_fn = _tag_exists_fn or compute_image_build.registry_tag_exists
    resolve_digest_fn = _resolve_digest_fn or compute_image_build._resolve_digest
    build_fn = _build_fn or build_project_compute_image
    record_fn = _record_fn or record_project_compute_image_build

    project_id = getattr(project, "id", None)
    try:
        owner = (getattr(project, "github_owner", None) or "").strip()
        repo = (getattr(project, "github_repo", None) or "").strip()
        if not owner or not repo:
            return {"status": "no_config", "reason": "no connected repo"}

        manifest = await manifest_fn(project)
        build_config = resolve_project_compute_image_build(
            (manifest or {}).get("compute")
        )
        if build_config is None:
            return {"status": "no_config", "reason": "no compute.image.build section"}

        token = await token_fn(owner, repo)
        if not token:
            return {"status": "no_config", "reason": "no installation read token"}

        default_branch = await default_branch_fn(token, owner, repo)
        project_id_str = str(project_id or "")
        dockerfile_rel = (
            build_config.get("dockerfile") or ".blueprint/Dockerfile.compute"
        )

        # Cheap current hash: fetch the Dockerfile + present dep files at the
        # default branch, NO clone. Absent Dockerfile => nothing buildable (the
        # real build would raise FileNotFoundError) => skip cleanly.
        dockerfile_bytes = await fetch_bytes_fn(
            dockerfile_rel,
            token=token,
            owner=owner,
            repo=repo,
            ref=default_branch,
            project_id=project_id_str,
        )
        if dockerfile_bytes is None:
            return {
                "status": "no_config",
                "reason": f"no Dockerfile at {dockerfile_rel}",
            }

        # Key inputs EXACTLY as build_and_push does: Dockerfile under
        # "Dockerfile", each present dep file under its relative path — so the
        # hash matches what a real build produces (an unchanged manifest =>
        # identical dep_hash => registry-tag-exists fast path).
        inputs: Dict[str, bytes] = {"Dockerfile": dockerfile_bytes}
        for rel in _DEFAULT_DEP_FILES:
            data = await fetch_bytes_fn(
                rel,
                token=token,
                owner=owner,
                repo=repo,
                ref=default_branch,
                project_id=project_id_str,
            )
            if data is not None:
                inputs[rel] = data

        dep_hash = compute_image_build.compute_dependency_hash(inputs)

        registry_repo = resolve_compute_image_registry_repo()
        image_name = (
            str(build_config["name"]).rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]
        )
        full_repo = f"{registry_repo}/{image_name}"

        # FAST-PATH NO-OP: the tag already exists => the image is current for this
        # exact manifest. Do NOT build; just make sure the pin is recorded.
        if await asyncio.to_thread(tag_exists_fn, full_repo, dep_hash):
            recorded = await _record_unchanged_pin(
                session,
                project_id,
                registry_repo,
                image_name,
                full_repo,
                dep_hash,
                resolve_digest_fn,
                record_fn,
            )
            return {"status": "unchanged", "dep_hash": dep_hash, "recorded": recorded}

        # A genuinely new/changed manifest: a real rebuild is needed.
        if not allow_build:
            # Per-cycle build budget exhausted — the next hourly pass retries.
            return {
                "status": "deferred",
                "reason": "build_budget_exhausted",
                "dep_hash": dep_hash,
            }

        # Respect the process-wide build concurrency guard the admin endpoint
        # uses: never stampede the orchestrator host with concurrent builds. If a
        # build is already in flight, skip building this cycle (retry next hour).
        acquired = False
        if build_semaphore is not None:
            if build_semaphore.locked():
                return {
                    "status": "deferred",
                    "reason": "builder_busy",
                    "dep_hash": dep_hash,
                }
            try:
                await asyncio.wait_for(
                    build_semaphore.acquire(), timeout=acquire_timeout
                )
                acquired = True
            except asyncio.TimeoutError:
                return {
                    "status": "deferred",
                    "reason": "builder_busy",
                    "dep_hash": dep_hash,
                }

        try:
            if progress_cb is not None:
                # Run the build as a background task with periodic progress
                # heartbeats so the reaper lifecycle watchdog
                # (REAPER_HANG_SECONDS=300) does not cancel the cycle during
                # a multi-minute baked-image build.
                _build_task = asyncio.ensure_future(build_fn(
                    project,
                    registry_repo=registry_repo,
                    build_config=build_config,
                    ref=default_branch,
                ))
                while True:
                    _done, _pending = await asyncio.wait(
                        [_build_task], timeout=_HEARTBEAT_INTERVAL
                    )
                    if _done:
                        result = _build_task.result()
                        break
                    progress_cb()
            else:
                result = await build_fn(
                    project,
                    registry_repo=registry_repo,
                    build_config=build_config,
                    ref=default_branch,
                )
        finally:
            if acquired:
                build_semaphore.release()

        result = await _preserve_existing_digest_pin_cb(session, project_id, result)
        row = await record_fn(
            session, project_id, registry_repo, image_name, result
        )
        await session.commit()
        logger.info(
            "[COMPUTE-IMAGE] auto-rebuilt project=%s dep_hash=%s (built=%s)",
            project_id,
            dep_hash,
            getattr(row, "built", None),
        )
        return {
            "status": "rebuilt",
            "dep_hash": dep_hash,
            "pinned_ref": getattr(row, "pinned_ref", None),
            "digest": getattr(row, "digest", None),
            "built": bool(getattr(row, "built", False)),
        }
    except Exception as e:  # noqa: BLE001 — best-effort; never raise into the sweep
        logger.warning(
            "[COMPUTE-IMAGE] auto-refresh failed for project=%s: %s",
            project_id,
            e,
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": str(e)}


def is_digest_pinned(ref: str | None) -> bool:
    """True when an image reference is content-addressed (contains ``@sha256:``).

    A digest-pinned reference (``registry/repo@sha256:...``) resolves to the same
    image layers on every pull, so two runs of the same compute_experiment are
    bit-identical over time. A mutable tag (``registry/repo:latest``) does not —
    it is the reproducibility hazard this predicate lets callers detect. ``None``
    / empty refs are not pinned.
    """
    return bool(ref) and "@sha256:" in ref


async def resolve_digest_pinned_default_image(
    session: Any,
    project_id: uuid.UUID,
    manifest_name: str | None,
) -> str | None:
    """Pin a project's mutable manifest compute-image tag to a content digest.

    ``compute.image.name`` in a project manifest is typically a *mutable* tag
    (e.g. ``registry/repo/my-project:latest``). Two runs of the same
    compute_experiment days apart can therefore resolve to different image
    layers, breaking A/B reproducibility (design gap G3). When a durable
    :class:`ProjectComputeImage` build record exists for this project, this
    resolves the tag to its content-addressed ``registry/repo@sha256:...``
    reference so repeated defaulting is bit-identical over time.

    Fail-open by design — the return value is a drop-in replacement for the raw
    ``manifest_name``:

    * ``None``/empty in -> ``None`` out (nothing to default).
    * already digest-pinned (contains ``@sha256:``) -> returned unchanged.
    * a matching ``built`` row with a non-null ``digest`` exists -> its
      ``pinned_ref``.
    * no such row -> the raw ``manifest_name`` unchanged (logs a debug note);
      this preserves today's behavior rather than introducing a new failure.

    **Matching is on the image's FULL identity — registry AND name — and an
    explicitly-tagged ref resolves to THAT tag's build.** A manifest ref
    ``registry/repo/name:tag`` names one specific image; its trailing ``name``
    does not. Keying the lookup on ``image_name`` alone (as this did) silently
    resolved to the wrong digest in two ordinary situations:

    * **Tag rollback.** A project pins ``:v2``, hits a regression, and rolls its
      manifest back to ``:v1``. Both builds carry the same ``image_name``, so the
      most-recently-updated row — the ``:v2`` build — still won, and the project
      kept running the image it had just rolled back, behind a digest-pinned ref
      that looked authoritative. An explicit tag now resolves to the build record
      pushed under that tag.
    * **A foreign image with the same trailing name.** Switching to an external
      prebuilt ``ghcr.io/vendor/my-project:latest`` matched this project's own
      ``…/compute-images/my-project`` build record and ran THAT digest. The
      registry is now part of the key, so a foreign image never matches a local
      build record; it fails open to the ref the manifest actually asked for.

    Resolution order for a ``registry``/``name``-matched row set:

    1. A row whose ``tag`` equals the ref's tag — an explicitly-pinned tag must
       resolve to its own build, never to a newer one.
    2. Otherwise the most recent build for that registry+name. A FLOATING tag
       (``:latest``, or no tag at all) is exactly the mutable-tag case this
       function exists to pin, and the pipeline pushes content-hash tags rather
       than ``latest``, so no per-tag row exists for it by construction — the
       latest build IS the right answer, and the pre-existing behavior.
    """
    if not manifest_name:
        return None
    if "@sha256:" in manifest_name:
        # Already content-addressed; nothing to pin.
        return manifest_name

    # Decompose the manifest ref into the same coordinates the build pipeline
    # persists on ProjectComputeImage (see build_project_compute_image /
    # record_project_compute_image_build), so the lookup keys on the image's full
    # identity rather than on its trailing name.
    registry_repo, image_name, ref_tag = _split_image_ref(manifest_name)

    try:
        from sqlalchemy import select

        conditions = [
            ProjectComputeImage.project_id == project_id,
            ProjectComputeImage.image_name == image_name,
            ProjectComputeImage.built.is_(True),
            ProjectComputeImage.digest.is_not(None),
        ]
        # A bare ``name`` with no registry prefix cannot be attributed to a
        # registry, so only the name is pinned down in that (unusual) case.
        if registry_repo:
            conditions.append(ProjectComputeImage.registry_repo == registry_repo)

        row = None
        if ref_tag:
            # (1) Explicit tag → the build pushed under exactly that tag.
            row = (
                await session.execute(
                    select(ProjectComputeImage)
                    .where(*conditions, ProjectComputeImage.tag == ref_tag)
                    .order_by(ProjectComputeImage.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if row is None:
            # (2) Floating/unknown tag → the latest build for this exact image.
            row = (
                await session.execute(
                    select(ProjectComputeImage)
                    .where(*conditions)
                    .order_by(ProjectComputeImage.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — fail-open to the raw tag on any DB error.
        logger.debug(
            "[COMPUTE] digest-pin lookup failed for project=%s image=%s; "
            "falling back to mutable tag",
            project_id,
            image_name,
        )
        return manifest_name

    if row is not None and row.pinned_ref:
        return row.pinned_ref

    # Fail-open to the mutable tag, but make it operator-VISIBLE: the images-first
    # substrate's core promise is run-to-run reproducibility via digest pinning,
    # and defaulting to a mutable tag silently defeats it. A warning here (not a
    # debug note) is the signal the operator needs to know a run they believe is
    # digest-reproducible is not. Callers additionally stamp the fact durably onto
    # the child payload via is_digest_pinned().
    logger.warning(
        "[COMPUTE] no digest-pinned build record for project=%s image=%s; "
        "defaulting to MUTABLE tag %s — run-to-run reproducibility is NOT "
        "guaranteed for this image",
        project_id,
        image_name,
        manifest_name,
    )
    return manifest_name


async def resolve_project_default_container_image(
    session: Any,
    project: Any,
) -> str | None:
    """Resolve a project's default baked compute image to a digest-pinned ref.

    The SINGLE shared implementation of the manifest-fetch + digest-pin idiom
    used to default a ``compute_experiment``'s ``container_image`` to the
    project's declared baked image. Both the completion authors
    (``plan_completion`` / ``research_completion``) and the orchestrator dispatch
    path (``_dispatch_compute_experiment_directly``) call this so the two cannot
    drift — a compute_experiment gets the same digest-pinned default whether it
    is authored through a completion handler or dispatched directly (operator
    experiment, planned prerequisite, retry).

    Reads the project's ``.blueprint/tools.json`` ``compute.image.name`` manifest
    entry and, when present, pins the (typically mutable) tag to a
    content-addressed ``registry/repo@sha256:...`` reference via
    :func:`resolve_digest_pinned_default_image`.

    Fail-OPEN and additive: returns ``None`` when the project is missing, its
    manifest declares no compute image, or any manifest/registry error occurs —
    the caller then preserves its current image-less behavior. Never raises.
    """
    if project is None:
        return None
    try:
        from app.routers.admin_project_tools import _discover_manifest_tools
        manifest = await _discover_manifest_tools(project)
        compute_image = (manifest.get("compute") or {}).get("image") or {}
        name = compute_image.get("name") if isinstance(compute_image, dict) else None
        if not name:
            return None
        # resolve_digest_pinned_default_image is idempotent (already-digest-pinned
        # refs return unchanged), so double-resolution across authoring + dispatch
        # is safe.
        return await resolve_digest_pinned_default_image(session, project.id, name)
    except Exception:  # noqa: BLE001 — fail-open to image-less behavior.
        logger.debug(
            "[COMPUTE] project default container-image resolution failed for "
            "project=%s",
            getattr(project, "id", None),
        )
        return None


def resolve_compute_image_registry_repo() -> str:
    """Return the Artifact Registry repo base for compute images.

    Reads ``COMPUTE_IMAGE_REGISTRY`` from the environment first; when absent,
    derives it from ``GCP_PROJECT_ID`` and ``COMPUTE_IMAGE_REGION`` (defaulting
    to ``"us"``) using the repo declared in ``infra/terraform/registry.tf``:
    ``<region>-docker.pkg.dev/<project>/compute-images``.

    Pure function of env/config — testable via monkeypatch.
    """
    explicit = os.environ.get("COMPUTE_IMAGE_REGISTRY", "").strip()
    if explicit:
        return explicit
    region = os.environ.get("COMPUTE_IMAGE_REGION", "us").strip() or "us"
    project = storage.GCP_PROJECT_ID
    return f"{region}-docker.pkg.dev/{project}/compute-images"
