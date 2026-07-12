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
from typing import Any, Dict, Protocol, Sequence

from google.cloud import storage as gcs_storage

from app import compute
from app import storage
from app.models import ComputeJob, ComputeJobStatus, Project, ProjectComputeImage
from app.vm_lifecycle import _ensure_vm_deleted

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

logger = logging.getLogger(__name__)


def _load_result_json(result_path: str) -> Any:
    """Synchronous helper for run_preflight_smoke — offloaded via asyncio.to_thread
    so that file I/O and json.loads don't block the event loop."""
    return json.loads(Path(result_path).read_text(encoding="utf-8"))

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
# Designated result-file path the command writes; read back after it finishes.
_GCP_RESULT_FILE_PATH = "/tmp/blueprint_compute_result.json"
# On-VM artifact directory the GCP startup script provisions and uploads.
_GCP_ARTIFACT_DIR_PATH = "/tmp/blueprint_compute_artifacts"
_GCP_ARTIFACT_BUNDLE_PATH = "/tmp/blueprint_compute_artifacts.tar.gz"
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
_MAX_VIEWABLE_FILE_BYTES = 8 * 1024 * 1024  # 8 MiB per viewable file
_MAX_VIEWABLE_FILE_COUNT = 25  # at most N individually-addressable objects/job
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
        return ComputeLaunchResult(
            external_id=external_id,
            started_at=started_at,
            cost_rate_per_second=0.0,
            message=f"spawned local subprocess pid={proc.pid}",
            metadata={"pid": proc.pid, **artifact_meta},
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
                metadata={"zone": existing_zone, "artifact_uri": artifact_uri},
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
        ok = await compute.provision_ephemeral_vm(
            external_id,
            startup_script=_build_gcp_startup_script(
                spec, artifact_upload_url=artifact_upload_url
            ),
            machine_type=spec.machine_type,
            provisioning_model=spec.provisioning_model,
            accelerator=spec.accelerator,
            accelerator_count=spec.accelerator_count,
        )
        if not ok:
            raise RuntimeError("GCP compute-job VM provisioning failed")
        zone = await asyncio.to_thread(compute.get_vm_zone, external_id)
        metadata: Dict[str, Any] = {"artifact_uri": artifact_uri}
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
            return ComputeStatusResult(
                status=cjs,
                exit_code=exit_code if exit_code is not None else (0 if cjs == ComputeJobStatus.SUCCEEDED else None),
                finished_at=finished_at or _utcnow(),
                metrics=metrics,
                command_log=command_log,
                message="compute job succeeded" if cjs == ComputeJobStatus.SUCCEEDED else "compute job failed",
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
) -> str:
    """Return a shell prefix: ``docker run --rm [--gpus all] -v … -e … IMAGE``.

    The caller appends ``bash -lc <quoted-command>`` after this prefix.
    ``host_out_dir`` must exist on the host before docker run is called;
    it is bind-mounted at ``container_out_dir`` so the command's
    result.json and artifacts land at the same host paths the terminal-status
    reader already uses. ``extra_mounts`` adds further ``host:container`` bind
    mounts (e.g. the cloned repo) and ``workdir`` sets the in-container cwd.
    """
    result_file = f"{container_out_dir}/result.json"
    artifact_dir = f"{container_out_dir}/artifacts"
    parts = ["docker", "run", "--rm"]
    if gpus:
        parts += ["--gpus", "all"]
    parts += [
        "-v", shlex.quote(f"{host_out_dir}:{container_out_dir}"),
        "-e", shlex.quote(f"{RESULT_FILE_ENV}={result_file}"),
        "-e", shlex.quote(f"{ARTIFACT_DIR_ENV}={artifact_dir}"),
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
) -> str:
    state_dir = shlex.quote(_local_state_dir_for_external_id(external_id))
    pid_path = shlex.quote(_local_pid_path_for_external_id(external_id))
    terminal_path = shlex.quote(_local_terminal_path_for_external_id(external_id))
    result_path = shlex.quote(_local_result_path_for_external_id(external_id))
    artifact_dir = shlex.quote(_local_artifact_dir_for_external_id(external_id))
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
    if container_image:
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
    # The artifact dir is created but NOT removed: its contents are the durable
    # output channel and must survive the subprocess (and an idempotent relaunch).
    return f"""mkdir -p {state_dir}
mkdir -p {artifact_dir}
printf '%s\\n' "$$" > {pid_path}
rm -f {terminal_path}
rm -f {result_path}
rm -f {command_log_path}
export {RESULT_FILE_ENV}={result_path}
export {ARTIFACT_DIR_ENV}={artifact_dir}
{setup_step_block}
{container_setup_block}
EXIT_CODE=0
if python3 - \
  {command_literal} \
  {command_log_path} \
  {live_tail_path_local} <<'PY'; then
import base64, json, pathlib, subprocess, sys, threading

command = sys.argv[1]
log_path = pathlib.Path(sys.argv[2])
live_tail_path = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else None
log_path.parent.mkdir(parents=True, exist_ok=True)
proc = subprocess.Popen(
    ["bash", "-lc", command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
captured = bytearray()
truncated = False
_stop = threading.Event()

def _flush_tail():
    if live_tail_path is None:
        return
    snap = bytes(captured)
    tail = snap[-10240:]
    payload = json.dumps({{"text": tail.decode("utf-8", "replace"), "truncated": len(snap) > 10240 or truncated}}, ensure_ascii=False).encode()
    try:
        live_tail_path.write_bytes(base64.b64encode(payload))
    except OSError:
        pass

def _tail_worker():
    while not _stop.wait(30):
        _flush_tail()

_t = threading.Thread(target=_tail_worker, daemon=True)
_t.start()
assert proc.stdout is not None
while True:
    chunk = proc.stdout.read(8192)
    if not chunk:
        break
    if len(captured) < {_MAX_COMMAND_LOG_BYTES}:
        remaining = {_MAX_COMMAND_LOG_BYTES} - len(captured)
        if len(chunk) <= remaining:
            captured.extend(chunk)
        else:
            captured.extend(chunk[:remaining])
            truncated = True
    else:
        truncated = True
exit_code = proc.wait()
_stop.set()
if proc.stdout is not None:
    proc.stdout.close()
if captured or truncated:
    payload = {{
        "text": captured.decode("utf-8", "replace"),
        "truncated": truncated,
        "byte_count": len(captured),
    }}
    log_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
sys.exit(exit_code)
PY
  STATUS=SUCCEEDED
  EXIT_CODE=0
else
  STATUS=FAILED
  EXIT_CODE=$?
fi
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
    ``content_type`` / ``kind`` come from the extension classifier, ``data`` is
    the in-memory bytes (already size-capped), and ``size_bytes`` is ``len(data)``.
    """

    filename: str
    content_type: str
    kind: str
    data: bytes
    size_bytes: int


def _extract_viewable_candidates_from_bundle(
    raw: bytes,
) -> list[ViewableArtifactCandidate]:
    """Select viewable files from a ``.tar.gz`` bundle IN MEMORY (no extract-to-disk).

    Mirrors the safe path-matching pattern in
    :func:`_extract_terminal_record_from_bundle`: iterate members, classify by
    the file extension, and read each selected member's bytes directly from the
    archive. The object name is derived from the sanitized *relative* member path
    (via :func:`storage.sanitize_viewable_relpath`), NOT the bare basename, so a
    multi-arm sweep whose arms each write a same-named plot
    (``arm1/reward.png`` vs ``arm2/reward.png``) yields distinct candidates
    instead of collapsing to one. Enforces the per-file size cap and the
    total-count cap, dedupes by that sanitized relpath (first occurrence wins),
    and classifies each as ``image``/``video``. A traversal/absolute member path
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
                classified = storage.viewable_content_type_for_filename(member.name)
                if classified is None:
                    continue
                if member.size <= 0 or member.size > _MAX_VIEWABLE_FILE_BYTES:
                    continue
                relname = storage.sanitize_viewable_relpath(member.name)
                if relname is None or relname in seen:
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read(_MAX_VIEWABLE_FILE_BYTES + 1)
                if not data or len(data) > _MAX_VIEWABLE_FILE_BYTES:
                    continue
                content_type, kind = classified
                seen.add(relname)
                out.append(
                    ViewableArtifactCandidate(
                        filename=relname,
                        content_type=content_type,
                        kind=kind,
                        data=data,
                        size_bytes=len(data),
                    )
                )
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return []
    return out


def _collect_viewable_candidates_from_dir(
    artifact_dir: str,
) -> list[ViewableArtifactCandidate]:
    """Select viewable files by walking a local job's on-disk artifact dir.

    The on-disk analogue of :func:`_extract_viewable_candidates_from_bundle`:
    same viewable-extension filter, per-file size cap, and total-count cap. The
    object name (and dedupe key) is the sanitized path *relative to*
    ``artifact_dir`` (via :func:`storage.sanitize_viewable_relpath`), NOT the
    bare basename, so same-named plots under different arm subdirectories
    (``arm1/reward.png`` vs ``arm2/reward.png``) stay distinct instead of
    collapsing to one. Returns ``[]`` for a missing/empty/unreadable dir or on
    ANY OS error — never raises.
    """
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return []
    out: list[ViewableArtifactCandidate] = []
    seen: set[str] = set()
    try:
        for root, _dirs, files in os.walk(artifact_dir):
            for name in sorted(files):
                classified = storage.viewable_content_type_for_filename(name)
                if classified is None:
                    continue
                path = os.path.join(root, name)
                relpath = os.path.relpath(path, artifact_dir)
                relname = storage.sanitize_viewable_relpath(relpath)
                if relname is None or relname in seen:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size <= 0 or size > _MAX_VIEWABLE_FILE_BYTES:
                    continue
                try:
                    with open(path, "rb") as file_handle:
                        data = file_handle.read(_MAX_VIEWABLE_FILE_BYTES + 1)
                except OSError:
                    continue
                if not data or len(data) > _MAX_VIEWABLE_FILE_BYTES:
                    continue
                content_type, kind = classified
                seen.add(relname)
                out.append(
                    ViewableArtifactCandidate(
                        filename=relname,
                        content_type=content_type,
                        kind=kind,
                        data=data,
                        size_bytes=len(data),
                    )
                )
    except OSError:
        return out
    return out


async def _collect_viewable_artifact_candidates(
    job: ComputeJob,
) -> tuple[list[ViewableArtifactCandidate], int]:
    """Backend-aware selection of a terminal job's viewable files.

    Local/local-gpu jobs are read from the on-disk artifact dir that survives the
    subprocess; gcp and runner-subprocess jobs are read from the durable
    ``bundle.tar.gz`` (downloaded once, iterated in memory). Returns
    ``([], 0)`` for an unrecognised backend or a missing bundle.

    Candidates are sorted by sanitized filename for deterministic selection when
    the archive/walk order is arbitrary. Returns ``(selected, dropped)`` where
    ``dropped`` is the count of qualifying files exceeding ``_MAX_VIEWABLE_FILE_COUNT``.
    """
    if job.backend in (LOCAL_BACKEND_NAME, LOCAL_GPU_BACKEND_NAME):
        meta = job.polling_metadata or {}
        artifact_dir = str(meta.get("artifact_dir") or "").strip()
        if not artifact_dir:
            artifact_dir = _local_artifact_dir_for_external_id(job.external_compute_id)
        all_candidates = await asyncio.to_thread(
            _collect_viewable_candidates_from_dir, artifact_dir
        )
    elif job.backend in (GCP_BACKEND_NAME, RUNNER_SUBPROCESS_BACKEND_NAME):
        raw = await storage.download_compute_job_artifact_bundle(job.id)
        if not raw:
            return [], 0
        all_candidates = await asyncio.to_thread(
            _extract_viewable_candidates_from_bundle, raw
        )
    else:
        return [], 0
    all_candidates.sort(key=lambda c: c.filename)
    selected = all_candidates[:_MAX_VIEWABLE_FILE_COUNT]
    dropped = len(all_candidates) - len(selected)
    return selected, dropped


async def persist_viewable_artifacts(
    job: ComputeJob,
) -> tuple[list[dict[str, Any]], int]:
    """Persist a terminal job's viewable files as individually-addressable objects.

    Extracts the viewable files (png/jpg/jpeg/webp/gif/svg/mp4/webm) from the
    job's artifacts, uploads each under the allowlisted per-job prefix, and
    returns ``(manifest, dropped)`` — a list of
    ``{filename, storage_uri, content_type, size_bytes, kind}`` for durably stored
    files, and the count of qualifying files that exceeded ``_MAX_VIEWABLE_FILE_COUNT``
    and were not persisted.

    Fail-soft by contract: an empty/corrupt bundle, a missing dir, or ANY
    extraction/upload error degrades to ``([], 0)`` and NEVER raises,
    so viewable-artifact persistence can never block a job's terminal completion.
    Individual per-file upload failures are skipped; only durably-stored files
    appear in the returned manifest.
    """
    try:
        candidates, dropped = await _collect_viewable_artifact_candidates(job)
    except Exception:
        logger.warning(
            "Viewable-artifact extraction failed for compute job %s",
            getattr(job, "id", None),
            exc_info=True,
        )
        return [], 0
    if not candidates:
        return [], dropped
    manifest: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            uri = await storage.upload_compute_job_viewable_file(
                job.id,
                candidate.filename,
                candidate.data,
                candidate.content_type,
            )
        except Exception:
            logger.warning(
                "Viewable-artifact upload raised for compute job %s file %r",
                getattr(job, "id", None),
                candidate.filename,
                exc_info=True,
            )
            uri = None
        if not uri:
            continue
        manifest.append(
            {
                "filename": candidate.filename,
                "storage_uri": uri,
                "content_type": candidate.content_type,
                "size_bytes": candidate.size_bytes,
                "kind": candidate.kind,
            }
        )
    return manifest, dropped


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
    spec: ComputeLaunchSpec, *, artifact_upload_url: str
) -> str:
    env_lines = []
    for key, value in sorted((spec.env or {}).items()):
        env_lines.append(f"export {key}={shlex.quote(value)}")
    env_block = "\n".join(env_lines)
    cwd_line = f"cd {shlex.quote(spec.cwd)}" if spec.cwd else ""
    # Quoted on-VM paths the durable terminal record and the clone-fail branch
    # both reference, so assign them before repo_setup_block is built below.
    result_file = shlex.quote(_GCP_RESULT_FILE_PATH)
    artifact_dir = shlex.quote(_GCP_ARTIFACT_DIR_PATH)
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
            f'  kill "$TTL_PID" 2>/dev/null || true\n'
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
            f'  kill "$TTL_PID" 2>/dev/null || true\n'
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
            + '  kill "$TTL_PID" 2>/dev/null || true\n'
            + "  poweroff || shutdown -h now || true\n"
            + "  exit 1\n"
            + "fi\n"
        )
    else:
        command = shlex.quote(spec.command)
    artifact_bundle = shlex.quote(_GCP_ARTIFACT_BUNDLE_PATH)
    artifact_upload_url = shlex.quote(artifact_upload_url)
    upload_timeout = _GCP_ARTIFACT_UPLOAD_TIMEOUT_SECONDS
    max_log_bytes = _MAX_COMMAND_LOG_BYTES
    command_runner_b64 = _COMMAND_RUNNER_B64
    artifact_upload_b64 = _ARTIFACT_UPLOAD_B64
    terminal_record_b64 = _TERMINAL_RECORD_B64
    orchestrator_url_q = shlex.quote(compute.ORCHESTRATOR_URL)
    job_id_q = shlex.quote(str(spec.job_id))
    return f"""#!/bin/bash
set -u -o pipefail
TTL_SECONDS={deadline_seconds}
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
_COMPUTE_ORCHESTRATOR_URL={orchestrator_url_q}
_COMPUTE_JOB_ID={job_id_q}
_compute_heartbeat() {{
  if [ -s {live_tail_path} ]; then
    _LT_VAL=$(cat {live_tail_path} 2>/dev/null || true)
    if [ -n "$_LT_VAL" ]; then
      curl -sf --max-time 5 -X POST -H "Authorization: Bearer $_COMPUTE_JOB_ID" -H "Content-Type: application/json" -d '{{"live_output_tail": "'"$_LT_VAL"'"}}' "$_COMPUTE_ORCHESTRATOR_URL/api/agent/compute-jobs/$_COMPUTE_JOB_ID/heartbeat" >/dev/null 2>&1 || true
      return
    fi
  fi
  curl -sf --max-time 5 -X POST -H "Authorization: Bearer $_COMPUTE_JOB_ID" "$_COMPUTE_ORCHESTRATOR_URL/api/agent/compute-jobs/$_COMPUTE_JOB_ID/heartbeat" >/dev/null 2>&1 || true
}}
_compute_heartbeat
(
  while true; do
    sleep 55
    _compute_heartbeat
  done
) &
_HEARTBEAT_PID=$!
echo "{_SERIAL_STATUS_PREFIX}RUNNING"
{env_block}
export {RESULT_FILE_ENV}={result_file}
export {ARTIFACT_DIR_ENV}={artifact_dir}
rm -f {result_file}
rm -f {command_log_path}
mkdir -p {artifact_dir}
python3 /tmp/blueprint_terminal_record.py \
  {artifact_dir} FAILED 1 vm_terminated_before_completion {result_file} {command_log_path} \
  >/dev/null 2>&1 || true
{repo_setup_block}
{setup_step_block}
{cwd_line}
{docker_setup_block}EXIT_CODE=0
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
kill "$TTL_PID" "$_HEARTBEAT_PID" 2>/dev/null || true
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


def _parse_serial_status(serial_output: str) -> ComputeStatusResult | None:
    status_line: str | None = None
    exit_code: int | None = None
    reason: str | None = None
    metrics_b64: str | None = None
    command_log_b64: str | None = None
    release_echo_b64: str | None = None
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
    if status_line is None:
        return None
    if status_line == "RUNNING":
        return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
    metrics = _decode_result_metrics(metrics_b64)
    command_log = _decode_command_log(command_log_b64)
    echoed_release_sha = _decode_release_echo_sha(release_echo_b64)
    if status_line == "SUCCEEDED":
        return ComputeStatusResult(
            status=ComputeJobStatus.SUCCEEDED,
            message=reason,
            exit_code=exit_code,
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

    Callers are responsible for committing the session.
    """
    from sqlalchemy import select

    pinned_ref = getattr(result, "pinned_ref", None) or ""
    tag = getattr(result, "tag", None) or ""
    dep_hash = getattr(result, "dep_hash", None) or ""
    built = bool(getattr(result, "built", False))
    digest = _parse_digest_from_pinned_ref(pinned_ref)

    existing = (
        await session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project_id,
                ProjectComputeImage.dep_hash == dep_hash,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.registry_repo = registry_repo
        existing.image_name = image_name
        existing.pinned_ref = pinned_ref
        existing.digest = digest
        existing.tag = tag
        existing.built = built
        existing.updated_at = _utcnow()
        session.add(existing)
        return existing

    row = ProjectComputeImage(
        project_id=project_id,
        registry_repo=registry_repo,
        image_name=image_name,
        dep_hash=dep_hash,
        tag=tag,
        pinned_ref=pinned_ref,
        digest=digest,
        built=built,
    )
    session.add(row)
    return row


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
    """
    if not manifest_name:
        return None
    if "@sha256:" in manifest_name:
        # Already content-addressed; nothing to pin.
        return manifest_name

    # Short image name within the registry repo, derived identically to the
    # build pipeline (see build_project_compute_image) so it matches the
    # persisted ProjectComputeImage.image_name.
    image_name = manifest_name.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]

    try:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(ProjectComputeImage)
                .where(
                    ProjectComputeImage.project_id == project_id,
                    ProjectComputeImage.image_name == image_name,
                    ProjectComputeImage.built.is_(True),
                    ProjectComputeImage.digest.is_not(None),
                )
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

    logger.debug(
        "[COMPUTE] no digest-pinned build record for project=%s image=%s; "
        "defaulting to mutable tag %s",
        project_id,
        image_name,
        manifest_name,
    )
    return manifest_name


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
