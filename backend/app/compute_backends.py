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
import os
import shlex
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Protocol

from app import compute
from app.models import ComputeJob, ComputeJobStatus
from app.vm_lifecycle import _ensure_vm_deleted

_SERIAL_STATUS_PREFIX = "BLUEPRINT_COMPUTE_STATUS="
_SERIAL_EXIT_PREFIX = "BLUEPRINT_COMPUTE_EXIT_CODE="
_SERIAL_REASON_PREFIX = "BLUEPRINT_COMPUTE_REASON="

LOCAL_BACKEND_NAME = "local-subprocess"
GCP_BACKEND_NAME = "gcp"
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
    # Deterministically reserved external id, persisted on the row BEFORE the
    # external launch side-effect so a re-launch after a crash is idempotent.
    external_id: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    provisioning_model: str = "STANDARD"


@dataclass(slots=True)
class ComputeLaunchResult:
    external_id: str
    started_at: datetime
    cost_rate_per_second: float
    message: str | None = None
    # Extra fields merged into the job's polling_metadata (e.g. GCP zone, pid).
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ComputeStatusResult:
    status: ComputeJobStatus
    message: str | None = None
    exit_code: int | None = None
    finished_at: datetime | None = None


@dataclass(slots=True)
class ComputeCostResult:
    total_cost_usd: float
    measured_at: datetime


@dataclass(slots=True)
class _LocalProcessHandle:
    proc: subprocess.Popen
    started_at: datetime


class ComputeBackend(Protocol):
    name: str

    def external_id_for(self, job: ComputeJob) -> str:
        ...

    async def launch(self, spec: ComputeLaunchSpec) -> ComputeLaunchResult:
        ...

    async def poll_status(self, job: ComputeJob) -> ComputeStatusResult:
        ...

    async def poll_cost(self, job: ComputeJob, *, as_of: datetime) -> ComputeCostResult:
        ...

    async def terminate(self, job: ComputeJob, *, reason: str) -> bool:
        ...


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
        existing = self._processes.get(external_id)
        if existing is not None and existing.proc.poll() is None:
            # Idempotent re-launch: the reserved id is already running.
            return ComputeLaunchResult(
                external_id=external_id,
                started_at=existing.started_at,
                cost_rate_per_second=0.0,
                message=f"local subprocess already running pid={existing.proc.pid}",
            )
        persisted_pid = _local_subprocess_pid_for_external_id(external_id)
        if persisted_pid is not None and _is_pid_running(persisted_pid):
            return ComputeLaunchResult(
                external_id=external_id,
                started_at=_local_subprocess_started_at_for_external_id(external_id)
                or _utcnow(),
                cost_rate_per_second=0.0,
                message=f"adopted existing local subprocess pid={persisted_pid}",
                metadata={"pid": persisted_pid},
            )
        if _read_local_terminal_status_for_external_id(external_id) is not None:
            metadata: dict[str, Any] = {}
            if persisted_pid is not None:
                metadata["pid"] = persisted_pid
            return ComputeLaunchResult(
                external_id=external_id,
                started_at=_local_subprocess_started_at_for_external_id(external_id)
                or _utcnow(),
                cost_rate_per_second=0.0,
                message="adopted completed local subprocess via terminal marker",
                metadata=metadata,
            )
        started_at = _utcnow()
        os.makedirs(_local_state_dir_for_external_id(external_id), exist_ok=True)
        _write_local_state_text(
            _local_started_at_path_for_external_id(external_id),
            started_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        _remove_local_terminal_status_for_external_id(external_id)
        env = os.environ.copy()
        env.update(spec.env or {})
        proc = subprocess.Popen(
            [
                "bash",
                "-lc",
                _build_local_wrapper_command(
                    spec.command,
                    external_id=external_id,
                ),
            ],
            cwd=spec.cwd or None,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._processes[external_id] = _LocalProcessHandle(
            proc=proc, started_at=started_at
        )
        return ComputeLaunchResult(
            external_id=external_id,
            started_at=started_at,
            cost_rate_per_second=0.0,
            message=f"spawned local subprocess pid={proc.pid}",
            metadata={"pid": proc.pid},
        )

    async def poll_status(self, job: ComputeJob) -> ComputeStatusResult:
        handle = self._processes.get(job.external_compute_id or "")
        if handle is None:
            terminal = _read_local_terminal_status(job)
            if terminal is not None:
                return terminal
            pid = _local_subprocess_pid(job)
            if pid is None:
                return ComputeStatusResult(
                    status=ComputeJobStatus.FAILED,
                    message="local subprocess no longer tracked and no pid was persisted",
                    finished_at=_utcnow(),
                )
            if _is_pid_running(pid):
                return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
            terminal = _read_local_terminal_status(job)
            if terminal is not None:
                return terminal
            return ComputeStatusResult(
                status=ComputeJobStatus.FAILED,
                message=f"local subprocess pid={pid} is no longer running",
                finished_at=_utcnow(),
            )
        return_code = handle.proc.poll()
        if return_code is None:
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
        self._processes.pop(job.external_compute_id or "", None)
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
            pid = _local_subprocess_pid(job)
            if pid is None or not _is_pid_running(pid):
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
                metadata={"zone": existing_zone},
            )
        ok = await compute.provision_ephemeral_vm(
            external_id,
            startup_script=_build_gcp_startup_script(spec),
            machine_type=spec.machine_type,
            provisioning_model=spec.provisioning_model,
        )
        if not ok:
            raise RuntimeError("GCP compute-job VM provisioning failed")
        zone = await asyncio.to_thread(compute.get_vm_zone, external_id)
        metadata: Dict[str, Any] = {}
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
        serial = await asyncio.to_thread(compute.get_vm_serial_output, external_id, zone)
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
        # _ensure_vm_deleted records the orchestrator-initiated-delete marker
        # before its first GCP call, so the reaper never misclassifies an
        # intentional budget/deadline teardown as a spot preemption. It returns
        # False if the VM survived all retries; surface that so the monitor can
        # flag the row — the orphan sweep (compute-job-vm-* aware) is the safety
        # net that eventually reclaims a VM that outlived its immediate delete.
        return await _ensure_vm_deleted(external_id, reason=f"compute_job_{reason}")


_LOCAL_BACKEND = LocalSubprocessComputeBackend()
_GCP_BACKEND = GcpComputeBackend()


def get_compute_backend(name: str) -> ComputeBackend:
    if name == _LOCAL_BACKEND.name:
        return _LOCAL_BACKEND
    if name == _GCP_BACKEND.name:
        return _GCP_BACKEND
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
    if raw_status == "SUCCEEDED":
        return ComputeStatusResult(
            status=ComputeJobStatus.SUCCEEDED,
            message="local subprocess exited 0",
            exit_code=0 if exit_code is None else exit_code,
            finished_at=finished_at,
        )
    if exit_code is None:
        return ComputeStatusResult(
            status=ComputeJobStatus.FAILED,
            message="local subprocess exited with unknown code",
            finished_at=finished_at,
        )
    return ComputeStatusResult(
        status=ComputeJobStatus.FAILED,
        message=f"local subprocess exited with code {exit_code}",
        exit_code=exit_code,
        finished_at=finished_at,
    )


def _build_local_wrapper_command(command: str, *, external_id: str) -> str:
    state_dir = shlex.quote(_local_state_dir_for_external_id(external_id))
    pid_path = shlex.quote(_local_pid_path_for_external_id(external_id))
    terminal_path = shlex.quote(_local_terminal_path_for_external_id(external_id))
    command_literal = shlex.quote(command)
    return f"""mkdir -p {state_dir}
printf '%s\\n' "$$" > {pid_path}
rm -f {terminal_path}
EXIT_CODE=0
if bash -lc {command_literal}; then
  STATUS=SUCCEEDED
  EXIT_CODE=0
else
  STATUS=FAILED
  EXIT_CODE=$?
fi
FINISHED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > {terminal_path} <<EOF
STATUS=$STATUS
EXIT_CODE=$EXIT_CODE
FINISHED_AT=$FINISHED_AT
EOF
exit "$EXIT_CODE"
"""


def _local_state_dir_for_external_id(external_id: str | None) -> str:
    suffix = str(external_id or "unassigned")
    return os.path.join(_LOCAL_STATE_ROOT, suffix)


def _local_pid_path_for_external_id(external_id: str | None) -> str:
    return os.path.join(_local_state_dir_for_external_id(external_id), _LOCAL_PID_FILENAME)


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


def _build_gcp_startup_script(spec: ComputeLaunchSpec) -> str:
    env_lines = []
    for key, value in sorted((spec.env or {}).items()):
        env_lines.append(f"export {key}={shlex.quote(value)}")
    env_block = "\n".join(env_lines)
    cwd_line = f"cd {shlex.quote(spec.cwd)}" if spec.cwd else ""
    if spec.deadline_at is not None:
        deadline_seconds = max(
            60,
            int((spec.deadline_at - _utcnow()).total_seconds()) + 30,
        )
    else:
        deadline_seconds = 3600
    command = shlex.quote(spec.command)
    return f"""#!/bin/bash
set -u -o pipefail
TTL_SECONDS={deadline_seconds}
(
  sleep "$TTL_SECONDS"
  echo "{_SERIAL_REASON_PREFIX}startup_ttl"
  echo "{_SERIAL_STATUS_PREFIX}FAILED"
  echo "{_SERIAL_EXIT_PREFIX}124"
  poweroff || shutdown -h now || true
) &
TTL_PID=$!
echo "{_SERIAL_STATUS_PREFIX}RUNNING"
{env_block}
{cwd_line}
EXIT_CODE=0
if bash -lc {command}; then
  EXIT_CODE=0
  echo "{_SERIAL_REASON_PREFIX}completed"
  echo "{_SERIAL_STATUS_PREFIX}SUCCEEDED"
else
  EXIT_CODE=$?
  echo "{_SERIAL_REASON_PREFIX}command_failed"
  echo "{_SERIAL_STATUS_PREFIX}FAILED"
fi
echo "{_SERIAL_EXIT_PREFIX}$EXIT_CODE"
kill "$TTL_PID" 2>/dev/null || true
poweroff || shutdown -h now || true
exit "$EXIT_CODE"
"""


def _parse_serial_status(serial_output: str) -> ComputeStatusResult | None:
    status_line: str | None = None
    exit_code: int | None = None
    reason: str | None = None
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
    if status_line is None:
        return None
    if status_line == "RUNNING":
        return ComputeStatusResult(status=ComputeJobStatus.RUNNING)
    if status_line == "SUCCEEDED":
        return ComputeStatusResult(
            status=ComputeJobStatus.SUCCEEDED,
            message=reason,
            exit_code=exit_code,
            finished_at=_utcnow(),
        )
    return ComputeStatusResult(
        status=ComputeJobStatus.FAILED,
        message=reason or "compute-job startup script failed",
        exit_code=exit_code,
        finished_at=_utcnow(),
    )
