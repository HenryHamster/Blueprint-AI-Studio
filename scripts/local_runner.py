"""Blueprint local fleet runner.

Pull-based runner process for operator hardware. Polls the cloud orchestrator
for pinned tasks and spawns the Blueprint agent binary for each claimed task.

Usage:
    python3 scripts/local_runner.py

Required environment variables:
    BLUEPRINT_RUNNER_TOKEN      Token from POST /api/admin/projects/{id}/runners/register
    BLUEPRINT_ORCHESTRATOR_URL  Base URL, e.g. https://api.example.com

Optional environment variables:
    BLUEPRINT_RUNNER_MAX_CONCURRENT   Max simultaneous agent subprocesses (default: 2, floor 1)
    BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS
                                      Max simultaneous compute jobs (default: 2, floor 1).
                                      Agents and compute have SEPARATE capacity pools.
    BLUEPRINT_AGENT_BINARY            Path to agent entry point (auto-detected if absent).
                                        When unset, the runner first looks for
                                        agent/dist/index.js (node) then falls back to
                                        agent/src/index.ts via tsx.
    BLUEPRINT_AGENT_IMAGE             Prod agent container image (e.g. agent/Dockerfile build).
                                      When set AND docker is usable, read-only agent task
                                      types run in this image (prod-parity, non-root) instead
                                      of bwrap. Unset → bwrap-only (unchanged behavior).
    BLUEPRINT_AGENT_BACKEND           Agent backend preference: auto (default) | bwrap | docker.
                                      "bwrap" forces the host bwrap path even if an image is
                                      set (disables the docker agent backend).
    BLUEPRINT_RUNNER_POLL_INTERVAL    Base poll interval in seconds (default: 2, floor 0.5)
    BLUEPRINT_RUNNER_AGENT_MAX_SECONDS Wall-clock deadline for an agent subprocess before the
                                      reaper terminates its process group (default: 14400 = 4h,
                                      floor 60)
    BLUEPRINT_RUNNER_MAX_AUTH_FAILURES Consecutive heartbeat 401s before the runner exits
                                      non-zero (revoked token; default: 20, 0 disables)
    BLUEPRINT_RUNNER_MAX_NETWORK_FAILURES Consecutive heartbeat network errors before the
                                      runner exits non-zero (default: 20, 0 disables)
    BLUEPRINT_RUNNER_WORKSPACE_ROOT   Parent directory for per-task agent workspaces
                                      (default: <tmp>/blueprint-agent-workspaces)
    BLUEPRINT_RUNNER_KEEP_WORKSPACES  Keep per-task workspaces after a task exits
                                      (1/true/yes) for post-mortem debugging; default
                                      is to auto-remove them so disk does not fill up.
    BLUEPRINT_RUNNER_MIN_DISK_BYTES   Low-disk guard: stop claiming new agent work when
                                      free space on the workspace filesystem is below this
                                      many bytes (default: 0 = disabled)
    BLUEPRINT_RUNNER_MAX_CPU_LOAD     Host-load guard: stop claiming new agent tasks when
                                      the 1-minute load average meets or exceeds this value
                                      (float, default: 0 = disabled)
    BLUEPRINT_RUNNER_MAX_GPU_UTIL_PCT Host-load guard: stop claiming new compute jobs when
                                      max GPU utilization meets or exceeds this percentage
                                      (int 0–100, default: 0 = disabled)
    BLUEPRINT_RUNNER_GPU_INDICES      Comma-separated GPU-index allowlist (default: all)
    BLUEPRINT_RUNNER_GPUS_PER_JOB     GPUs assigned per compute job (default: 1)
    BLUEPRINT_RUNNER_CPU_LIMIT        Best-effort CPU cap (cores) per task
    BLUEPRINT_RUNNER_MEM_LIMIT        Best-effort memory cap per task (e.g. 8g)

ISOLATION (fail-closed): every task — agent or compute — runs inside an isolation
boundary or the runner declines it. There is NO bare/unsandboxed execution path.
See ``scripts/runner_isolation.py`` for the capability detection + decision table.
Run ``python3 scripts/local_runner.py --check`` to verify setup without claiming
work.

Flags:
    --check   Print the preflight report and exit (claims no work). Exit code 0
              iff the orchestrator is reachable, the token is valid, and at least
              one task class is claimable.
    --help/-h Print the RUNNER_RUNBOOK (recommended launch patterns) and exit.

RUNNER_RUNBOOK — recommended launch patterns
--------------------------------------------
The runner self-updates: when the orchestrator advertises a newer version and
the runner is idle, it updates and restarts. For that (and for crash recovery)
it MUST be under a process supervisor, or wrapped so it comes back:

  * systemd service (preferred) — auto-restarts on crash AND on self-update.
    Add ``Restart=always`` to the unit; the runner exits 0 after a self-update
    and systemd restarts the fresh binary.

  * tmux / nohup / screen (manual) — no supervisor. The self-updater detects
    this and re-execs in place (``os.execv``) so the UPDATE is not lost, but a
    genuine CRASH still needs a restarter. Wrap the runner in a restart loop:

        while true; do python3 scripts/local_runner.py; sleep 1; done

    Set ``BLUEPRINT_RUNNER_SUPERVISED=1`` in that loop's environment to tell the
    runner it IS supervised (it then exits cleanly on update instead of
    re-exec'ing, letting the loop respawn it).

``--check`` reports whether a supervisor was detected and warns if not.
"""

import io
import json
import typing
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import random
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

import runner_isolation as iso
import blueprint_runner as br


# ── Configuration ────────────────────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: required environment variable {name} is not set.", file=sys.stderr)
        print(f"  Set it and re-run: export {name}=<value>", file=sys.stderr)
        sys.exit(1)
    return value


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an int env var, tolerating garbage (clear warning, keep running).

    A malformed value (``BLUEPRINT_RUNNER_MAX_CONCURRENT=abc``) must NOT crash the
    runner into a systemd restart loop — it falls back to ``default`` with a loud
    warning. ``minimum`` clamps the result (e.g. floor concurrency at 1 so a value
    of 0/-1 can never wedge the runner into claiming nothing).
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            print(
                f"[runner] WARNING: {name}={raw!r} is not a valid integer — using default {default}.",
                flush=True,
            )
            value = default
    if minimum is not None and value < minimum:
        if raw is not None and raw.strip() != "":
            print(
                f"[runner] WARNING: {name}={value} below minimum {minimum} — clamping to {minimum}.",
                flush=True,
            )
        value = minimum
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    """Parse a float env var, tolerating garbage (clear warning, keep running)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = float(raw.strip())
        except (TypeError, ValueError):
            print(
                f"[runner] WARNING: {name}={raw!r} is not a valid number — using default {default}.",
                flush=True,
            )
            value = default
    if minimum is not None and value < minimum:
        if raw is not None and raw.strip() != "":
            print(
                f"[runner] WARNING: {name}={value} below minimum {minimum} — clamping to {minimum}.",
                flush=True,
            )
        value = minimum
    return value


def _parse_retry_after(value) -> float | None:
    """Parse an HTTP ``Retry-After`` header into seconds, or None if unparseable.

    Honors both accepted forms (RFC 7231): a delta-seconds integer, or an
    HTTP-date. A negative/past value clamps to 0. Anything unparseable returns
    None so the caller falls back to its default back-off.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        secs = float(text)
        return secs if secs >= 0 else 0.0
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return delta if delta >= 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return None


def _utc_now_iso() -> str:
    """Current UTC time as a compact ISO-8601 string (e.g. 2026-07-02T12:34:56Z)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _die(reason: str, code: int = 1) -> None:
    """Log a fatal reason and hard-exit the PROCESS (not just a thread).

    ``os._exit`` is used deliberately: this is called from daemon threads (the
    heartbeat's revoked-token detector, the thread supervisor) where ``sys.exit``
    would only unwind the calling thread and leave the process silently degraded.
    A non-zero exit surfaces in systemd/journalctl and lets the process supervisor
    restart the runner.

    Before exiting, write a minimal status.json so a post-mortem on-box inspection
    shows the fatal condition — the operator can ``cat ~/.blueprint/status.json``
    and see what killed the runner without needing journalctl access.
    """
    print(f"[runner] FATAL: {reason}", file=sys.stderr, flush=True)
    try:
        _write_status_json(
            runner_id=_hostname(),
            last_heartbeat_status=f"fatal: {reason[:120]}",
            active_task_ids=[],
            active_compute_job_ids=[],
        )
    except Exception:
        pass  # best-effort only — must not block the exit
    os._exit(code)


def _detect_process_supervisor() -> str | None:
    """Return the name of the process supervisor managing this runner, or None.

    A supervisor is what restarts the runner after it exits (crash or clean
    exit-for-restart following a self-update). If NONE is detected, the runner
    was almost certainly launched manually (tmux/nohup/screen) and a plain
    ``os._exit`` leaves nothing to bring it back — the dgx-1 ~45min-outage
    failure mode. Detection is intentionally broad and conservative: any
    positive signal returns a supervisor name; only the genuinely-unsupervised
    case returns None.

    Signals checked (any hit wins):
      * ``INVOCATION_ID`` / ``JOURNAL_STREAM`` env → launched by systemd.
      * ``SUPERVISOR_ENABLED`` env → launched by supervisord.
      * PID 1 is systemd/supervisord/runit (``/proc/1/comm``) AND we are a
        child chain of it — a containerized runner with an init supervisor.
      * ``BLUEPRINT_RUNNER_SUPERVISED=1`` → operator-declared restart wrapper
        (documented in the RUNNER_RUNBOOK so a ``while true`` loop can opt in).
    """
    # systemd sets these for units it launches.
    if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
        return "systemd"
    # supervisord exports this to managed children.
    if os.environ.get("SUPERVISOR_ENABLED"):
        return "supervisord"
    # Operator-declared restart wrapper (see RUNNER_RUNBOOK). Lets a manual
    # `while true; do local_runner ...; done` loop tell the runner it IS
    # supervised so it exits cleanly instead of re-exec'ing in place.
    if os.environ.get("BLUEPRINT_RUNNER_SUPERVISED") == "1":
        return "declared"
    # An init-style PID 1 (systemd/supervisord/runit) — typical of a container
    # whose entrypoint is a supervisor that will restart this process.
    try:
        with open("/proc/1/comm", "r") as fh:
            pid1 = fh.read().strip()
        if pid1 in ("systemd", "supervisord", "runit", "runsv", "s6-svscan", "tini"):
            return pid1
    except Exception:
        pass
    return None


def _reexec_after_update() -> None:
    """Replace the current process with the updated runner, or exit for restart.

    Called after a successful auto-update. If a process supervisor is present,
    ``os._exit(0)`` (the supervisor restarts the fresh binary). If NONE is
    detected (manual tmux/nohup launch), ``os.execv`` re-execs in place so the
    updated code takes over WITHOUT relying on an external restarter — the fix
    for the silent post-update death. Any failure in the re-exec path falls back
    to ``os._exit(0)`` so we never leave a hung, half-updated process.
    """
    supervisor = None
    try:
        supervisor = _detect_process_supervisor()
    except Exception:
        supervisor = None
    if supervisor is not None:
        print(
            f"[runner] auto-update complete — {supervisor} supervisor detected, "
            "exiting for restart.",
            flush=True,
        )
        os._exit(0)
    # No supervisor: re-exec in place so the update is not silently lost.
    print(
        "[runner] auto-update complete — NO process supervisor detected "
        "(manual tmux/nohup/screen launch); re-exec'ing in place so the update "
        "takes effect. Wrap the runner in systemd or a restart loop to recover "
        "from crashes too (see --check RUNNER_RUNBOOK).",
        flush=True,
    )
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(
            f"[runner] re-exec failed ({exc!r}) — exiting for restart instead.",
            flush=True,
        )
        os._exit(0)


# ── Credential persistence ────────────────────────────────────────────────────

def _credential_dir() -> str:
    """Directory for the persisted runner credential file.

    Defaults to ``~/.blueprint/``. Override via ``BLUEPRINT_CREDENTIAL_DIR`` for
    testing or non-standard installs.
    """
    return os.environ.get("BLUEPRINT_CREDENTIAL_DIR", "").strip() or os.path.expanduser("~/.blueprint")


def _credential_path() -> str:
    """Full path to the persisted runner token file."""
    return os.path.join(_credential_dir(), "runner_token")


def _save_runner_token(token: str) -> None:
    """Persist the runner token to a 0600-permission credential file.

    Writes atomically (temp file + rename) so a concurrent reader never sees a
    partial write. Silent best-effort — a write failure must not abort startup
    when the runner already authenticated via the env-var token.
    """
    path = _credential_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.chmod(os.path.dirname(path), 0o700)
        except OSError:
            pass
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            os.write(fd, token.encode())
        finally:
            os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception as exc:
        print(
            f"[runner] WARNING: could not persist runner token to {path}: {exc}",
            flush=True,
        )


def _load_runner_token() -> str | None:
    """Read the persisted runner token from the credential file.

    Returns None if the file does not exist, is empty, or cannot be read.
    """
    path = _credential_path()
    try:
        with open(path) as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None


def _write_status_json(
    runner_id: str | None,
    last_heartbeat_status: str,
    active_task_ids: list,
    active_compute_job_ids: list,
    *,
    caps: "iso.Capabilities | None" = None,
    agent_backend: str | None = None,
    compute_backend: str | None = None,
) -> None:
    """Write a compact on-box status file so an operator can triage an offline runner.

    The file is written atomically (temp file + rename, 0o644) to
    ``~/.blueprint/status.json`` (respects ``BLUEPRINT_CREDENTIAL_DIR``).  It
    contains no secrets — only the runner identity, liveness, active work, and
    detected isolation capabilities.  An operator can ``cat`` it to answer "when did
    it last heartbeat?" or "what was it working on?" even when the process is dead.

    Called from the heartbeat loop after every POST (success or failure) so the
    file's mtime is the last time the process wrote anything, and from ``_die()``
    to leave a post-mortem snapshot of the fatal condition.
    """
    path = os.path.join(_credential_dir(), "status.json")
    last_heartbeat_at = _utc_now_iso()
    payload: dict = {
        "runner_id": runner_id,
        "last_heartbeat_at": last_heartbeat_at,
        "last_heartbeat_status": last_heartbeat_status,
        "active_task_ids": list(active_task_ids),
        "active_compute_job_ids": list(active_compute_job_ids),
    }
    if caps is not None:
        payload["bwrap_ok"] = caps.bwrap_ok
        payload["docker_usable"] = caps.docker_usable
        payload["agent_backend"] = agent_backend
        payload["compute_backend"] = compute_backend
    else:
        payload["bwrap_ok"] = None
        payload["docker_usable"] = None
        payload["agent_backend"] = None
        payload["compute_backend"] = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            os.write(fd, json.dumps(payload, sort_keys=True).encode())
        finally:
            os.close(fd)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception as exc:
        print(
            f"[runner] WARNING: could not write status.json to {path}: {exc}",
            flush=True,
        )


def _hostname() -> str | None:
    """Best-effort host name for this runner, sent on every heartbeat.

    The orchestrator stores this on the runner row so the operator console shows
    a real machine name instead of a bare id/None. Returns ``socket.gethostname()``
    or None if it cannot be determined — the heartbeat schema tolerates a missing
    hostname (old runners omit it), so a failure here never breaks the heartbeat.
    """
    try:
        name = socket.gethostname()
    except OSError:
        return None
    return name or None


def _runner_version() -> str | None:
    """Best-effort runner version from ``git describe`` in the scripts directory.

    Sent on every heartbeat so the operator's fleet view can see which build each
    heterogeneous device is running (a stale runner is a common cause of a device
    silently declining a task class). Returns a short version string (tag/sha,
    ``-dirty`` when the working tree has uncommitted changes) or ``None`` on ANY
    failure — git absent, not a repo, timeout — so a version probe never breaks a
    heartbeat (the schema tolerates a missing version; old runners omit it).
    """
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=scripts_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    version = result.stdout.strip()
    return version or None


def _describe_sha(version_str: str) -> str | None:
    """Extract the abbreviated commit SHA from a ``git describe`` output string.

    Parses output like ``v1.2.3-5-g4f5a6b7`` → ``4f5a6b7`` so the runner can
    compare against the orchestrator's full-SHA ``version_hint``.  Returns
    ``None`` when no commit SHA is present (e.g. a pure tag like ``v1.2.3``),
    in which case the caller falls back to a substring comparison.
    """
    clean = version_str.replace("-dirty", "")
    # git describe format: <tag>-<N>-g<abbreviated-sha>
    if "-g" in clean:
        return clean.split("-g")[-1]
    # --always fallback when no tags exist: bare abbreviated SHA
    if clean and all(c in "0123456789abcdef" for c in clean):
        return clean
    # Pure tag — no commit SHA to compare
    return None


def _node_runtime_ok() -> bool:
    """Return True iff ``node --version`` actually runs (binary present + executable).

    A FUNCTIONAL probe, not a file-exists check: a broken or absent node runtime
    must not let the runner advertise ``agent_available=True`` while every agent
    task then dies at spawn. Any failure (absent binary, non-zero exit, timeout)
    reads as not-ready.
    """
    try:
        result = subprocess.run(
            ["node", "--version"], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _docker_agent_possible(caps: "iso.Capabilities", agent_image: str | None) -> bool:
    """Whether the DOCKER agent backend can run agent work on this host.

    Requires ``BLUEPRINT_AGENT_IMAGE`` set AND a usable docker daemon. The agent
    runtime + node live INSIDE the prod image, so a host agent binary / node is
    NOT required here (unlike bwrap). This is the coarse advertisement gate: the
    per-task read-only enforcement (only read-only agent types get docker) is done
    by ``iso.decide_agent_backend(task_type=...)`` at claim time. When
    ``agent_image`` is None this is always False → behavior is bwrap-only, i.e.
    identical to before the docker agent backend existed.
    """
    return bool(agent_image) and bool(caps.docker_usable)


def _agent_image_present(agent_image: str) -> bool:
    """Whether ``docker image inspect <agent_image>`` succeeds (image locally present).

    Guards against a wrong/absent ``BLUEPRINT_AGENT_IMAGE`` that would otherwise
    advertise "ready" and drain agent tasks whose container run then dies with
    nothing reported — stranding/looping the task (claim-and-crash-loop). Called
    ONCE at startup in ``main()`` (NOT per-heartbeat — that would shell out on every
    beat). Any failure (missing image, docker error, timeout) reads as absent, so
    the caller disables the docker-agent path and falls back to bwrap-or-decline.
    """
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", agent_image],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _disk_free_bytes(root: str | None) -> int | None:
    """Free bytes on the filesystem holding *root*, or None on any error."""
    if not root:
        return None
    try:
        return int(shutil.disk_usage(root).free)
    except (OSError, ValueError):
        return None


def _gpu_free_vram_mib() -> list[int] | None:
    """Per-GPU free VRAM in MiB for all GPUs on this host, best-effort/nullable.

    Queries ``nvidia-smi --query-gpu=memory.free``. Returns a list with one entry
    per GPU (index-aligned with gpu_names). Returns None on any failure.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    vals: list[int] = []
    for line in result.stdout.splitlines():
        tok = line.strip()
        if not tok:
            continue
        try:
            vals.append(max(0, int(float(tok))))
        except ValueError:
            continue
    return vals if vals else None


def _gpu_utilization_pct() -> int | None:
    """Max GPU utilization (0–100) across this host's GPUs, best-effort/nullable.

    Queries ``nvidia-smi --query-gpu=utilization.gpu``. Any failure (no GPU, no
    nvidia-smi, timeout, unparseable output) returns None.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    vals: list[int] = []
    for line in result.stdout.splitlines():
        tok = line.strip()
        if not tok:
            continue
        try:
            vals.append(int(float(tok)))
        except ValueError:
            continue
    if not vals:
        return None
    return max(0, min(100, max(vals)))


def _cpu_load_avg() -> float | None:
    """One-minute CPU load average, or None on any error."""
    try:
        load1, _5min, _15min = os.getloadavg()
        return round(load1, 2)
    except (OSError, AttributeError):
        return None


def _memory_free_bytes() -> int | None:
    """Free (available) memory in bytes, or None on any error.

    Reads ``/proc/meminfo`` for ``MemAvailable`` (preferred) or falls
    back to ``MemFree``.  Returns None when neither can be read.
    """
    try:
        with open("/proc/meminfo") as f:
            fallback: int | None = None
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
                if line.startswith("MemFree:"):
                    fallback = int(line.split()[1]) * 1024
                    # MemAvailable is preferred but may not exist on all kernels;
                    # MemFree is the fallback when we didn't see MemAvailable first.
                    # Guard: MemAvailable *follows* MemFree in /proc/meminfo, so we
                    # cannot return early — keep scanning for MemAvailable.
            return fallback
    except (OSError, ValueError, FileNotFoundError, IndexError):
        return None


class _TelemetryState:
    """Thread-safe runner health counters emitted on each heartbeat.

    Updated on every job completion (agent or compute): a success stamps
    ``last_successful_job_at`` and resets the consecutive-failure counter; a
    failure increments it. Operator-initiated outcomes (cancel, graceful
    shutdown) are neutral — they are neither a success nor a health failure.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_successful_job_at: str | None = None
        self.consecutive_job_failures: int = 0
        self._pause_start_at: float | None = None

    def record_success(self) -> None:
        with self._lock:
            self.last_successful_job_at = _utc_now_iso()
            self.consecutive_job_failures = 0
            self._pause_start_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_job_failures += 1

    def snapshot(self) -> tuple[str | None, int]:
        with self._lock:
            return self.last_successful_job_at, self.consecutive_job_failures

    def pause_remaining(self, threshold: int, pause_seconds: float) -> float | None:
        """Check and update the circuit-breaker pause state.

        Returns None if no pause should apply (threshold disabled or
        consecutive failures below threshold). Returns the remaining
        pause duration in seconds (> 0) when the circuit-breaker is active,
        meaning the caller should NOT claim new agent work.

        The caller must sleep for at least ``min(remaining, poll_interval)``
        before re-checking; when the pause expires this method returns None,
        allowing claiming to resume. A successful task (``record_success()``)
        clears both the failure count and the pause state.
        """
        with self._lock:
            if threshold <= 0 or self.consecutive_job_failures < threshold:
                self._pause_start_at = None
                return None
            if self._pause_start_at is None:
                self._pause_start_at = time.monotonic()
                return pause_seconds
            elapsed = time.monotonic() - self._pause_start_at
            if elapsed >= pause_seconds:
                self._pause_start_at = None
                return None
            return pause_seconds - elapsed


def _supervise_threads(threads: dict) -> None:
    """Make a dead background loop LOUD + fatal instead of a silent degradation.

    Each background loop wraps its body so a transient error can't kill it; if one
    still dies (a bug, or an error outside the wrapped region) while we are NOT
    shutting down, the runner is silently half-broken — e.g. a dead reaper leaks
    capacity permits forever. Exit non-zero so the process supervisor restarts a
    clean runner.
    """
    if _shutdown_event.is_set():
        return
    for name, th in threads.items():
        if th is not None and not th.is_alive():
            _die(
                f"background thread '{name}' died unexpectedly — exiting non-zero so the "
                "process supervisor (systemd) restarts the runner."
            )


def _locate_agent_binary() -> list[str] | None:
    """Locate the agent entry argv, or return None if it is unavailable.

    Non-fatal counterpart to ``_find_agent_binary``.  The runner main loop uses
    this to resolve the agent LAZILY — only when an AGENT task is actually
    claimed — so a compute/GPU-only runner can boot and run compute jobs without
    a built agent.  A missing binary is therefore not an error at startup; it is
    only a problem the moment an agent task needs to run.

    Returns the full argv (``["node", "/path/...js"]`` or
    ``["/path/tsx", "/path/...ts"]``) so callers can use it directly as the
    subprocess command.

    Resolution order:
      1. ``BLUEPRINT_AGENT_BINARY`` env var (an explicit path — run with node).
      2. ``<script_dir>/../agent/dist/index.js`` — compiled dist artifact.
      3. ``<cwd>/agent/dist/index.js`` — same, relative to working directory.
      4. ``<script_dir>/../agent/src/index.ts`` via
         ``<agent_dir>/node_modules/.bin/tsx`` — tsx-based fallback.
      5. ``<cwd>/agent/src/index.ts`` via ``tsx`` on PATH — same, cwd-relative.
    """
    explicit = os.environ.get("BLUEPRINT_AGENT_BINARY", "").strip()
    if explicit:
        return (
            ["node", explicit]
            if os.path.isfile(explicit)
            else None
        )

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Check for compiled dist/index.js first (node, fast).
    for base in [os.path.join(script_dir, ".."), os.getcwd()]:
        dist_path = os.path.normpath(os.path.join(base, "agent", "dist", "index.js"))
        if os.path.isfile(dist_path):
            return ["node", dist_path]

    # tsx fallback: resolve tsx from agent/node_modules/.bin/tsx, or rely on PATH.
    for base in [os.path.join(script_dir, ".."), os.getcwd()]:
        agent_dir = os.path.normpath(os.path.join(base, "agent"))
        entry_ts = os.path.join(agent_dir, "src", "index.ts")
        if not os.path.isfile(entry_ts):
            continue
        tsx_candidate = os.path.join(agent_dir, "node_modules", ".bin", "tsx")
        if os.path.isfile(tsx_candidate):
            return [tsx_candidate, entry_ts]
        # Fallback: hope tsx is on PATH.
        return ["tsx", entry_ts]

    return None


def _find_agent_binary() -> list[str]:
    """Resolve the agent entry argv or exit(1) (strict, hard-fail variant).

    Retained for callers/tests that require a hard failure when the binary is
    absent.  The runner main loop no longer calls this at startup — it resolves
    lazily via ``_locate_agent_binary`` so a compute-only runner is not blocked
    by a missing agent build.
    """
    located = _locate_agent_binary()
    if located:
        return located

    explicit = os.environ.get("BLUEPRINT_AGENT_BINARY", "").strip()
    if explicit:
        print(f"ERROR: BLUEPRINT_AGENT_BINARY={explicit!r} does not exist.", file=sys.stderr)
        sys.exit(1)

    print(
        "ERROR: could not locate the agent entry point. "
        "If a compiled dist/index.js exists, it will be used with node. "
        "Otherwise the runner looks for agent/src/index.ts and runs it via tsx "
        "(from agent/node_modules/.bin/tsx or PATH). "
        "Set BLUEPRINT_AGENT_BINARY to the entry point path to override auto-detection.",
        file=sys.stderr,
    )
    sys.exit(1)


def _agent_workspace_path(task_id: str) -> str:
    """Compute the per-task workspace path for *task_id* WITHOUT creating it.

    Split out from ``_agent_workspace_for_task`` so cleanup can derive the same
    directory to remove after a task exits, without the side effect of re-creating
    it via ``os.makedirs``.
    """
    root = os.environ.get("BLUEPRINT_RUNNER_WORKSPACE_ROOT", "").strip()
    if not root:
        root = os.path.join(tempfile.gettempdir(), "blueprint-agent-workspaces")
    safe_task_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)
    if not safe_task_id:
        safe_task_id = "unknown"
    return os.path.abspath(os.path.join(root, safe_task_id))


def _agent_workspace_for_task(task_id: str) -> str:
    """Return a per-task workspace path for an agent subprocess.

    The node agent defaults to /tmp/workspace when AGENT_WORKSPACE is absent.
    A persistent local runner may execute multiple tasks across one machine
    session, so each task gets a stable isolated workspace directory.
    """
    workspace = _agent_workspace_path(task_id)
    os.makedirs(workspace, exist_ok=True)
    return workspace


def _cleanup_agent_workspace(task_id: str) -> None:
    """Best-effort removal of a finished task's per-task workspace.

    Unlike compute-job scratch dirs (rmtree'd after each job), per-task agent
    workspaces are never reused once the agent exits. Leaving them behind on a
    long-lived runner silently exhausts disk, which then trips the disk-based
    degraded-runner health path and takes the runner offline. Reap the workspace
    when the agent finishes. Set BLUEPRINT_RUNNER_KEEP_WORKSPACES=1 to retain
    them for post-mortem debugging.
    """
    if os.environ.get("BLUEPRINT_RUNNER_KEEP_WORKSPACES", "").strip().lower() in (
        "1", "true", "yes",
    ):
        return
    try:
        shutil.rmtree(_agent_workspace_path(task_id), ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — cleanup is strictly best-effort
        print(f"[runner] workspace cleanup error for task {task_id}: {exc}", flush=True)


def _min_free_disk_bytes() -> int:
    """Low-disk guard threshold in bytes (0 disables the guard).

    Read from BLUEPRINT_RUNNER_MIN_DISK_BYTES. When free space on the workspace
    filesystem drops below this, the agent poll loop stops claiming new tasks so
    a nearly-full disk fails fast instead of starting work that will die mid-run.
    """
    raw = os.environ.get("BLUEPRINT_RUNNER_MIN_DISK_BYTES", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _disk_below_min(root: str | None, min_free_bytes: int) -> bool:
    """True iff *root*'s free space is known and below *min_free_bytes* (>0)."""
    if min_free_bytes <= 0:
        return False
    free = _disk_free_bytes(root)
    return free is not None and free < min_free_bytes


def _compute_image_keep_days() -> int:
    """Minimum age (days) of Docker images protected from cache-hygiene pruning.

    Configurable via ``BLUEPRINT_RUNNER_IMAGE_KEEP_DAYS`` (default 7). Images
    younger than this — and any image backing a running container — are never
    pruned. The dgx-1 has ~30 GB of image-layer storage, so unbounded growth of
    per-project compute images would eventually wedge the runner; this bounds it
    without evicting images that a recent or in-flight job still needs.
    """
    raw = os.environ.get("BLUEPRINT_RUNNER_IMAGE_KEEP_DAYS", "").strip()
    if not raw:
        return 7
    try:
        return max(0, int(raw))
    except ValueError:
        return 7


def _prune_docker_images(
    workspace_root: str | None,
    min_free_bytes: int,
    *,
    keep_days: int | None = None,
    _run=subprocess.run,
    _disk_below=None,
) -> dict:
    """Best-effort: reclaim Docker image storage when disk runs low.

    Runs ONLY when *workspace_root*'s free space is below *min_free_bytes* — the
    same guard the agent-claim path uses (:func:`_disk_below_min`). Delegates to
    ``docker image prune -a`` with an ``until=<keep_days>`` filter, so Docker
    removes only images that are (a) not referenced by any running/active
    container and (b) older than the keep window. Images backing an in-flight
    compute job run inside a live container and are therefore protected; recent
    images (last *keep_days*) are also kept.

    Returns a small status dict for logging/testing. Never raises — a missing
    ``docker`` binary or a prune error degrades to ``{"pruned": False, ...}``.
    """
    keep = _compute_image_keep_days() if keep_days is None else keep_days
    disk_below = _disk_below if _disk_below is not None else _disk_below_min
    if not disk_below(workspace_root, min_free_bytes):
        return {"pruned": False, "reason": "disk_above_threshold"}
    until_hours = max(1, keep * 24)
    try:
        proc = _run(
            [
                "docker",
                "image",
                "prune",
                "-a",
                "-f",
                "--filter",
                f"until={until_hours}h",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"pruned": False, "reason": f"prune_error:{type(e).__name__}"}
    if proc.returncode != 0:
        return {
            "pruned": False,
            "reason": "prune_nonzero",
            "stderr": (proc.stderr or "")[-500:],
        }
    return {
        "pruned": True,
        "keep_days": keep,
        "output": (proc.stdout or "").strip()[-500:],
    }


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _post(url: str, token: str, body: dict, *, return_headers: bool = False):
    """POST JSON body.

    Default return is ``(status_code, parsed_body_or_None)``. When
    ``return_headers`` is set, returns a 3-tuple ``(status, body, retry_after)``
    where ``retry_after`` is the parsed ``Retry-After`` header in seconds (or
    None) — used by the poll loops to honor a 429 rate-limit back-off instead of
    a fixed sleep.
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    def _pack(status, parsed, headers):
        if return_headers:
            retry_after = _parse_retry_after(headers.get("Retry-After")) if headers else None
            return status, parsed, retry_after
        return status, parsed

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            headers = getattr(resp, "headers", None)
            raw = resp.read()
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = None
            return _pack(status, parsed, headers)
    except urllib.error.HTTPError as exc:
        headers = getattr(exc, "headers", None)
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return _pack(exc.code, parsed, headers)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


# ── Compute-job constants ─────────────────────────────────────────────────────

_RUNNER_CONTAINER_OUT_DIR = "/tmp/blueprint_out"
# Total command-log budget reported to the orchestrator. Matches the backend's
# own _MAX_RUNNER_COMMAND_LOG_BYTES so a captured log is never re-truncated on
# arrival. Raised from 32768 now that consecutive-duplicate-line collapsing keeps
# the captured text dense (see _BoundedDedupLog).
_RUNNER_MAX_COMMAND_LOG_BYTES = 65536
# Bytes of the log HEAD always retained (run setup / environment). The remainder
# of the budget is a rolling TAIL — test suites and training loops print their
# pass/fail summary at the END, so a head-only cap discarded exactly the results a
# downstream verdict needs.
_RUNNER_COMMAND_LOG_HEAD_BYTES = 8192
_RUNNER_HEARTBEAT_INTERVAL = 30.0
# Minimum pause duration for the task-claiming circuit-breaker.
# When BLUEPRINT_RUNNER_FAILURE_PAUSE_THRESHOLD consecutive job failures are
# reached, the runner pauses claiming new agent tasks for this many seconds.
# A successful task clears the failure count and resumes normal claiming.
_FAILURE_PAUSE_SECONDS = 60.0
# v1 cap on durable artifact uploads. The runner skips (does not fail) any run
# whose artifact dir exceeds this; the backend mirrors the cap and 413s.
_RUNNER_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB

# Durable-retry policy for TERMINAL compute-job reports and artifact uploads.
# These POSTs carry the measured result_metrics / final exec_status and the run's
# debugging artifacts; a single transient network blip at job end would silently
# lose them, turning a job that actually ran into a spurious
# compute_unmeasured_criterion. Terminal reports and uploads therefore retry with
# capped exponential backoff. Interim "running" heartbeats are NOT retried — the
# next heartbeat re-sends them.
_REPORT_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
_REPORT_MAX_RETRIES = 4
_REPORT_RETRY_BASE_SLEEP = 2.0
_REPORT_RETRY_MAX_SLEEP = 30.0


def _report_retry_sleep(attempt: int) -> float:
    """Capped exponential backoff (seconds) for the terminal-report retry loop."""
    return min(_REPORT_RETRY_BASE_SLEEP * (2 ** attempt), _REPORT_RETRY_MAX_SLEEP)


class _BoundedDedupLog:
    """Stream-capture command output with two defenses against noisy logs.

    1. Consecutive-duplicate-line collapsing — a burst of an identical line is
       stored ONCE followed by a ``[previous line repeated N more time(s)]``
       marker. This is content-agnostic (no hardcoded patterns): it collapses the
       unsuppressable CUDA ``'+ptxNN' is not a recognized feature`` warning a newer
       GPU spams in an older container, and any other repeated spam, so noise can
       never crowd out the run's real output.
    2. Head+tail byte budget — the first ``head_bytes`` and the last
       ``max_bytes - head_bytes`` bytes are always kept; the middle is elided. The
       previous head-only cap dropped the END of the log, which is exactly where a
       test suite or training loop prints its pass/fail summary — the result a
       downstream verdict agent must reason over.

    Fed arbitrary byte chunks via :meth:`feed` (from the reader thread); :meth:`result`
    returns ``(text, truncated)`` where ``truncated`` is True iff content was elided.
    ``feed`` and ``result`` are serialized by a lock so ``result`` may be called while
    the reader thread is still delivering output (the runner joins the reader with a
    timeout, so it can still be alive).

    The assembled text — including the elision marker — never exceeds ``max_bytes``
    (the head+tail budgets reserve room for the marker), so the orchestrator's own
    equal-size cap never re-truncates it on arrival.
    """

    # Reserve for the "...[log middle elided...]..." marker so head+marker+tail
    # stays within max_bytes (the marker is ~49 bytes; round up).
    _ELISION_MARKER = b"\n...[log middle elided to fit capture budget]...\n"
    _ELISION_RESERVE = 64

    def __init__(self, max_bytes: int, head_bytes: int) -> None:
        self._max_bytes = max(0, max_bytes)
        usable = max(0, self._max_bytes - self._ELISION_RESERVE)
        self._head_budget = max(0, min(head_bytes, usable))
        self._tail_budget = max(0, usable - self._head_budget)
        self._pending = bytearray()           # buffered incomplete trailing line
        self._head = bytearray()
        self._head_full = False
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self._elided = False
        self._last_line: bytes | None = None   # last DISTINCT line (sans newline)
        self._repeat = 0
        self._lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self._pending.extend(chunk)
            while True:
                nl = self._pending.find(b"\n")
                if nl < 0:
                    break
                line = bytes(self._pending[: nl + 1])  # keep the newline on storage
                del self._pending[: nl + 1]
                self._emit_line(line)
            # Bound the unflushed partial line: a newline-free stream (e.g. '\r'
            # progress bars, which reach feed() because stderr is merged into
            # stdout) must not buffer the whole stream in memory. Once the partial
            # line exceeds the total budget, force-flush it as a synthetic line —
            # _store/_push_tail then clip it to the byte budget.
            if len(self._pending) > self._max_bytes:
                forced = bytes(self._pending)
                self._pending = bytearray()
                self._emit_line(forced)

    def _emit_line(self, line: bytes) -> None:
        key = line.rstrip(b"\n")
        if self._last_line is not None and key == self._last_line:
            self._repeat += 1
            return
        self._flush_repeat()
        self._last_line = key
        self._store(line)

    def _flush_repeat(self) -> None:
        if self._repeat > 0:
            marker = (
                f"  [previous line repeated {self._repeat} more time(s)]\n"
            ).encode("utf-8")
            self._repeat = 0
            self._store(marker)

    def _store(self, data: bytes) -> None:
        if not self._head_full and len(self._head) < self._head_budget:
            take = self._head_budget - len(self._head)
            self._head.extend(data[:take])
            if len(data) > take:
                self._head_full = True
                self._push_tail(data[take:])
        else:
            self._head_full = True
            self._push_tail(data)

    def _push_tail(self, data: bytes) -> None:
        if self._tail_budget <= 0:
            if data:
                self._elided = True
            return
        # A single item larger than the whole tail budget can never be trimmed by
        # popping (we always keep >=1 item). Keep only its LAST tail_budget bytes —
        # the most recent output (end-of-run results) — so the buffer honors the
        # budget instead of growing unbounded and mis-reporting truncated=False.
        if len(data) > self._tail_budget:
            data = data[-self._tail_budget:]
            self._elided = True
        self._tail.append(data)
        self._tail_bytes += len(data)
        while self._tail_bytes > self._tail_budget and len(self._tail) > 1:
            self._tail_bytes -= len(self._tail.popleft())
            self._elided = True

    def result(self) -> tuple[str, bool]:
        with self._lock:
            if self._pending:
                self._emit_line(bytes(self._pending))
                self._pending = bytearray()
            self._flush_repeat()
            head = bytes(self._head)
            tail = b"".join(self._tail)
            if self._elided:
                text = (
                    head.decode("utf-8", "replace")
                    + self._ELISION_MARKER.decode("utf-8")
                    + tail.decode("utf-8", "replace")
                )
                return text, True
            return (head + tail).decode("utf-8", "replace"), False


# ── GPU detection ────────────────────────────────────────────────────────────

def _detect_gpu_compute_capabilities() -> list:
    """Query CUDA compute capability for each GPU via nvidia-smi.

    Returns a list of floats (e.g. [8.9] for an NVIDIA L40S, [7.0] for V100).
    Returns an empty list on any error so the caller can proceed without it.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        caps = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line:
                try:
                    caps.append(float(line))
                except ValueError:
                    pass
        return caps
    except Exception:
        return []


def _detect_gpu() -> dict:
    """Detect GPU hardware by probing nvidia-smi.

    Returns a dict with has_gpu, gpu_count, gpu_names, and
    gpu_compute_capabilities (list of float CUDA compute capability per GPU,
    e.g. [8.9] for an L40S).  Any error (binary absent, timeout, non-zero
    exit) is treated as no GPU present so detection is always non-fatal.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {"has_gpu": False, "gpu_count": 0, "gpu_names": [], "gpu_compute_capabilities": []}
        import re
        gpu_names = re.findall(r"^GPU \d+:\s*(.+?)(?:\s*\(UUID:.+)?\s*$", result.stdout, re.MULTILINE)
        if gpu_names:
            compute_caps = _detect_gpu_compute_capabilities()
            return {"has_gpu": True, "gpu_count": len(gpu_names), "gpu_names": gpu_names, "gpu_compute_capabilities": compute_caps}
        return {"has_gpu": False, "gpu_count": 0, "gpu_names": [], "gpu_compute_capabilities": []}
    except Exception:
        return {"has_gpu": False, "gpu_count": 0, "gpu_names": [], "gpu_compute_capabilities": []}


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_shutdown_event = threading.Event()
# Separate from _shutdown_event — used during auto-update so background
# threads (reaper, compute poller, main loop) are NOT signaled to exit.
_update_in_progress = threading.Event()


def _handle_signal(signum, frame):  # noqa: ARG001
    if not _shutdown_event.is_set():
        print("\n[runner] Signal received — initiating graceful shutdown.", flush=True)
        _shutdown_event.set()


def _wait_for_compute_jobs(
    active_compute_jobs: set[str],
    compute_lock: threading.Lock,
    orchestrator_url: str,
    token: str,
    timeout: float = 120,
) -> None:
    """Wait for active compute jobs to finish during graceful shutdown.

    Compute job worker threads are daemon threads (they would be killed on
    process exit).  This wait gives them a chance to finish their current
    command and report a final status to the orchestrator.  Any jobs still
    active after the deadline are reported as failed so they do not remain
    in ``running`` status indefinitely — there is no server-side sweep for
    stale ComputeJob rows.
    """
    with compute_lock:
        remaining = set(active_compute_jobs)

    if not remaining:
        return

    print(
        f"[runner] Shutdown requested — waiting for {len(remaining)} "
        "active compute jobs...",
        flush=True,
    )
    deadline = time.monotonic() + timeout
    while remaining and time.monotonic() < deadline:
        time.sleep(0.5)
        with compute_lock:
            remaining = set(active_compute_jobs)

    if remaining:
        print(
            f"[runner] timeout waiting for {len(remaining)} compute jobs "
            "— reporting as failed",
            flush=True,
        )
        for job_id in remaining:
            try:
                _report_compute_job(
                    orchestrator_url,
                    token,
                    job_id,
                    exec_status="failed",
                    exit_code=1,
                    command_log={
                        "text": "Runner shut down (SIGTERM) before job completed",
                        "truncated": False,
                    },
                )
            except Exception:
                print(
                    f"[runner] failed to report stranded compute job {job_id} "
                    "— continuing shutdown",
                    flush=True,
                )


# ── Active task tracking (background thread) ──────────────────────────────────

def _release_capacity(capacity: threading.Semaphore | None) -> None:
    if capacity is not None:
        capacity.release()


def _notify_decline(orchestrator_url: str, token: str, task_id: str) -> None:
    """Best-effort: tell the orchestrator this runner declined a claimed task.

    When the runner claims a task from /next-task but then declines it at the
    isolation-backend level (no bwrap, no docker, capability mismatch), the
    orchestrator still has it pinned in PROVISIONING.  Without this notification
    the task strands until the stale-runner sweep fires when THIS runner goes
    offline — which may be hours or never.  This call releases the pin so the
    dispatch loop can re-pin it next cycle.
    """
    try:
        url = f"{orchestrator_url}/api/runner/decline-task"
        _post(url, token, {"task_id": task_id})
    except Exception:
        pass  # best-effort — the stale-runner sweep is the safety net


def _try_acquire_capacity(capacity: threading.Semaphore | None) -> bool:
    if capacity is None:
        return True
    return capacity.acquire(blocking=False)


def _reap_loop(
    active_tasks: dict,
    lock: threading.Lock,
    capacity: threading.Semaphore | None = None,
    *,
    agent_started_at: dict | None = None,
    agent_max_seconds: float | None = None,
    telemetry: "_TelemetryState | None" = None,
    agent_teardown: dict | None = None,
) -> None:
    """Reap finished agents and terminate any that blow past their wall-clock deadline.

    The loop body is wrapped so a transient error never silently kills the reaper
    thread (a dead reaper would leak capacity permits forever); the ``main()``
    supervisor makes an actually-dead thread fatal.
    """
    _cleanup_tick = 0
    while not _shutdown_event.is_set():
        try:
            now = time.monotonic()
            # (task_id, proc, backend, container_name) for the docker-aware teardown.
            overdue: list[tuple[str, object, "str | None", "str | None"]] = []
            with lock:
                done = [tid for tid, proc in active_tasks.items() if proc.poll() is not None]
                for tid in done:
                    proc = active_tasks[tid]
                    code = proc.returncode
                    del active_tasks[tid]
                    if agent_started_at is not None:
                        agent_started_at.pop(tid, None)
                    if agent_teardown is not None:
                        # Cleanly-exited agent: the docker run was attached, so the
                        # container is already gone — just drop the tracking entry.
                        agent_teardown.pop(tid, None)
                    _release_capacity(capacity)
                    if telemetry is not None:
                        if code == 0:
                            telemetry.record_success()
                        elif code == 42:
                            # Exit 42 = runtime_unavailable (missing CLI binary).
                            # This is an infra decline, not a task failure — do NOT
                            # increment the failure streak so the runner never trips
                            # degraded=job_failure_streak from a missing-CLI event.
                            pass
                        else:
                            telemetry.record_failure()
                    if code == 42:
                        print(f"[DONE] task_id={tid} exit_code=42 (runtime_unavailable — not a failure)", flush=True)
                    else:
                        print(f"[DONE] task_id={tid} exit_code={code}", flush=True)
                    # Reap the per-task workspace so stale dirs don't accumulate
                    # and silently exhaust the runner's disk (unlike compute-job
                    # scratch dirs, these are never otherwise removed).
                    _cleanup_agent_workspace(tid)

                # Wall-clock deadline: an agent that never exits (wedged tool, runaway
                # loop) must not hold a capacity permit forever. Mirror the compute
                # deadline machinery. Remove under the lock; terminate outside it
                # (terminate_process_tree can block up to its SIGTERM→SIGKILL grace).
                if agent_started_at is not None and agent_max_seconds:
                    for tid, proc in list(active_tasks.items()):
                        started = agent_started_at.get(tid)
                        if started is not None and (now - started) >= agent_max_seconds:
                            backend, cname = (
                                (agent_teardown or {}).get(tid, (None, None))
                            )
                            overdue.append((tid, proc, backend, cname))
                    for tid, _proc, _b, _c in overdue:
                        del active_tasks[tid]
                        agent_started_at.pop(tid, None)
                        if agent_teardown is not None:
                            agent_teardown.pop(tid, None)

            for tid, proc, backend, container_name in overdue:
                print(
                    f"[runner] agent task {tid} exceeded {agent_max_seconds:.0f}s wall-clock "
                    f"deadline - terminating process group (SIGTERM then SIGKILL).",
                    flush=True,
                )
                try:
                    # For the docker backend this also issues `docker stop <name>` so
                    # the container is not orphaned (the client group signal alone
                    # never reaches the daemon-run workload). Mirrors compute teardown.
                    iso.terminate_process_tree(
                        proc, backend=backend, container_name=container_name
                    )
                except Exception as exc:  # noqa: BLE001 — always settle capacity below
                    print(f"[runner] error terminating overdue agent {tid}: {exc}", flush=True)
                _release_capacity(capacity)
                if telemetry is not None:
                    telemetry.record_failure()
                # Terminated overdue agent is done too — reap its workspace.
                _cleanup_agent_workspace(tid)

            # Periodic stale workspace cleanup (every ~60 s).
            _cleanup_tick += 1
            if _cleanup_tick % 30 == 0:
                with lock:
                    active_ids = set(active_tasks.keys())
                removed = _cleanup_stale_workspaces(active_ids)
                if removed:
                    print(f"[runner] cleaned up {removed} stale workspace(s)", flush=True)
            # Periodic stale task log cleanup (every ~5 min) — 150 ticks × 2 s.
            if _cleanup_tick % 150 == 0:
                log_removed = _cleanup_stale_task_logs()
                if log_removed:
                    print(f"[runner] cleaned up {log_removed} stale task log(s)", flush=True)
        except Exception as exc:  # noqa: BLE001 — never let the reaper thread die silently
            print(f"[runner] reap loop error (continuing): {exc}", flush=True)
        time.sleep(2)


# ── Heartbeat (background thread) ─────────────────────────────────────────────

def _heartbeat_loop(
    orchestrator_url: str,
    token: str,
    active_tasks: dict,
    lock: threading.Lock,
    gpu_capabilities: dict,
    active_compute_jobs: set | None = None,
    compute_lock=None,
    agent_sandbox_ok: bool = True,
    telemetry: "_TelemetryState | None" = None,
    workspace_root: str | None = None,
    max_auth_failures: int = 0,
    max_network_failures: int = 0,
    caps: "iso.Capabilities | None" = None,
    agent_image: str | None = None,
) -> None:
    url = f"{orchestrator_url}/api/runner/heartbeat"
    has_gpu = bool(gpu_capabilities.get("has_gpu"))
    # Host identity is stable for the process lifetime — resolve it once and send
    # it on every heartbeat so the console shows the real machine name (the runner
    # is enrolled from the browser, which cannot know the host's name, so this is
    # the only place the hostname is populated).
    hostname = _hostname()
    # Runner version is stable for the process lifetime — resolve it once (a git
    # describe shell-out) and send it on every heartbeat so the fleet view can show
    # which build each device runs. Best-effort: None when git/the repo is absent.
    version = _runner_version()
    consecutive_401 = 0
    consecutive_network_failures = 0
    while not _shutdown_event.is_set():
        try:
            with lock:
                task_ids = list(active_tasks.keys())
            if active_compute_jobs is not None and compute_lock is not None:
                with compute_lock:
                    compute_job_ids = sorted(active_compute_jobs)
            else:
                compute_job_ids = []
            # FAIL-CLOSED advertisement across BOTH agent backends:
            #  * bwrap  — needs the HOST toolchain: a bwrap sandbox (bwrap+userns,
            #    passed in as agent_sandbox_ok) AND the agent dist AND a working node
            #    runtime. A file-exists check is not enough — a broken/absent node
            #    would show "ready" while every agent task dies at spawn.
            #  * docker — needs BLUEPRINT_AGENT_IMAGE + a usable docker daemon; the
            #    runtime + agent live in the prod image, so the host node/binary are
            #    irrelevant. Only read-only agent types actually run here; that
            #    per-type gate is enforced at claim time by decide_agent_backend.
            # If NEITHER leg is satisfiable, agent_available=False and the
            # orchestrator stops pinning agent tasks here. When agent_image is None
            # this collapses to the prior bwrap-only behavior (no regression).
            agent_binary_present = _locate_agent_binary() is not None
            bwrap_agent_ok = (
                agent_sandbox_ok
                and agent_binary_present
                and _node_runtime_ok()
            )
            # A docker-only box (docker+image WITHOUT bwrap) advertises
            # agent_available=True but can run ONLY the read-only slice
            # (iso.AGENT_DOCKER_TYPES). It self-restricts by DECLARING that slice in
            # `supported_agent_task_types` below; the orchestrator intersects that
            # runner-declared floor with the operator-configured task_types ceiling
            # (backend orchestrator._runner_handles_task_type), so a write/heavy type
            # is never pinned here (it would DECLINE at claim time and strand/loop).
            # The operator `task_types` field is still preserved server-side — this is
            # a distinct runner capability, not a clobber of operator config.
            docker_agent_ok = caps is not None and _docker_agent_possible(caps, agent_image)
            agent_available = bwrap_agent_ok or docker_agent_ok
            if agent_available:
                agent_unavailable_reason = None
            elif not agent_sandbox_ok:
                agent_unavailable_reason = "bwrap not available"
            elif not agent_binary_present:
                agent_unavailable_reason = "agent binary not found"
            elif not _node_runtime_ok():
                agent_unavailable_reason = "node runtime not functional"
            else:
                agent_unavailable_reason = "docker agent unavailable"
            # Runner-DECLARED agent capability floor. A docker-ONLY box (docker+image
            # but NO bwrap) can only run the read-only agent slice — the prod agent
            # image is read-only-parity and write/heavy types would DECLINE at claim
            # time and strand/loop. Declare exactly that slice so the orchestrator
            # intersects it with the operator policy and never pins this box a type it
            # cannot fulfil. When bwrap is available (the full agent slice runs on the
            # host toolchain) or there is no agent backend at all, declare None → no
            # runner-side restriction, byte-identical to the prior behavior.
            supported_agent_task_types = (
                sorted(iso.AGENT_DOCKER_TYPES)
                if (docker_agent_ok and not bwrap_agent_ok)
                else None
            )
            last_success_at, consecutive_failures = (
                telemetry.snapshot() if telemetry is not None else (None, 0)
            )
            # Isolation-backend inventory + selected backends for per-device
            # observability. Gated on caps: an old caller may pass caps=None, in
            # which case these serialize as null (the schema tolerates them absent).
            if caps is not None:
                isolation_backends = iso.capabilities_inventory(caps)
                agent_backend = iso.decide_agent_backend(
                    caps, agent_binary_present=agent_binary_present,
                    agent_image=agent_image,
                )
                compute_backend = iso.decide_compute_backend(
                    caps, has_image=agent_image is not None
                )
                llm_clis = iso.probe_llm_clis()
            else:
                isolation_backends = None
                agent_backend = None
                compute_backend = None
                llm_clis = None
            body = {
                "active_task_ids": task_ids,
                "active_compute_job_ids": compute_job_ids,
                "agent_available": agent_available,
                "agent_unavailable_reason": agent_unavailable_reason,
                "supported_agent_task_types": supported_agent_task_types,
                "hostname": hostname,
                "version": version,
                "isolation_backends": isolation_backends,
                "agent_backend": agent_backend,
                "compute_backend": compute_backend,
                "llm_clis": llm_clis,
                # Health telemetry consumed by the orchestrator to spot a runner
                # that is failing every job / running low on disk / low on memory /
                # pegged CPU.
                "last_successful_job_at": last_success_at,
                "consecutive_job_failures": consecutive_failures,
                "disk_free_bytes": _disk_free_bytes(workspace_root),
                "cpu_load_avg": _cpu_load_avg(),
                "memory_free_bytes": _memory_free_bytes(),
                "gpu_utilization_pct": _gpu_utilization_pct() if has_gpu else None,
                "gpu_free_vram_mib": _gpu_free_vram_mib() if has_gpu else None,
                **gpu_capabilities,
            }
            status, resp_body = _post(url, token, body)
            if status in (200, 204):
                consecutive_401 = 0
                consecutive_network_failures = 0
                # Parse orchestrator version hint so the runner can detect staleness.
                if isinstance(resp_body, dict):
                    version_hint = resp_body.get("version_hint")
                    if version_hint and version:
                        runner_sha = _describe_sha(version)
                        if runner_sha:
                            stale = not version_hint.startswith(runner_sha)
                        else:
                            stale = version_hint not in version
                        if stale:
                            # Check if runner is completely idle before attempting
                            # auto-update (no active tasks and no compute jobs).
                            with lock:
                                has_tasks = bool(active_tasks)
                            if active_compute_jobs is not None and compute_lock is not None:
                                with compute_lock:
                                    has_compute = bool(active_compute_jobs)
                            else:
                                has_compute = False

                            if not has_tasks and not has_compute:
                                print(
                                    f"[runner] version mismatch ({version!r} vs "
                                    f"{version_hint!r}) and runner is idle — "
                                    "auto-updating...",
                                    flush=True,
                                )
                                _update_in_progress.set()
                                try:
                                    result = br.do_update()
                                except Exception:
                                    _update_in_progress.clear()
                                    raise
                                if result == 0:
                                    # Update succeeded. _restart_runner_service
                                    # (called inside do_update) will kill this
                                    # process via systemctl restart if a service
                                    # unit is installed. If still alive (no
                                    # systemd), _reexec_after_update re-execs in
                                    # place when NO supervisor is detected (the
                                    # manual tmux/nohup launch that otherwise
                                    # dies silently), else exits for restart.
                                    _reexec_after_update()
                                else:
                                    print(
                                        f"[runner] auto-update failed "
                                        f"(exit={result}) — continuing with "
                                        "current version.",
                                        flush=True,
                                    )
                                    _update_in_progress.clear()
                            else:
                                print(
                                    f"[runner] WARNING: runner version {version!r} "
                                    f"does not match orchestrator version hint "
                                    f"{version_hint!r} — not auto-updating because "
                                    "tasks are still executing.",
                                    flush=True,
                                )
                _write_status_json(
                    runner_id=hostname,
                    last_heartbeat_status="ok",
                    active_task_ids=task_ids,
                    active_compute_job_ids=compute_job_ids,
                    caps=caps,
                    agent_backend=agent_backend,
                    compute_backend=compute_backend,
                )
            elif status == 401:
                # Before counting this as an auth failure, re-read the token file
                # so the operator can rotate a revoked token without restarting the
                # runner (update the file → runner auto-detects → resumes heartbeats).
                new_token = _load_runner_token()
                if not new_token:
                    new_token = os.environ.get("BLUEPRINT_RUNNER_TOKEN", "").strip() or None
                if new_token and new_token != token:
                    print(
                        "[runner] heartbeat 401 — detected updated token on disk; "
                        "resetting auth counter and resuming with new token.",
                        flush=True,
                    )
                    token = new_token
                    consecutive_401 = 0
                    consecutive_network_failures = 0
                    continue  # retry immediately with the new token
                consecutive_401 += 1
                print(
                    f"[runner] heartbeat 401 ({consecutive_401} consecutive) — token "
                    "rejected; will re-arm if the row was merely inactive.",
                    flush=True,
                )
                _write_status_json(
                    runner_id=hostname,
                    last_heartbeat_status=f"error: 401 ({consecutive_401} consecutive)",
                    active_task_ids=task_ids,
                    active_compute_job_ids=compute_job_ids,
                    caps=caps,
                    agent_backend=agent_backend,
                    compute_backend=compute_backend,
                )
                if max_auth_failures and consecutive_401 >= max_auth_failures:
                    _die(
                        f"heartbeat rejected with 401 for {consecutive_401} consecutive "
                        "attempts — runner token is likely revoked. Exiting non-zero so "
                        "the operator/supervisor notices instead of looping silently."
                    )
            else:
                print(f"[runner] heartbeat warning: HTTP {status}", flush=True)
                _write_status_json(
                    runner_id=hostname,
                    last_heartbeat_status=f"error: HTTP {status}",
                    active_task_ids=task_ids,
                    active_compute_job_ids=compute_job_ids,
                    caps=caps,
                    agent_backend=agent_backend,
                    compute_backend=compute_backend,
                )
        except Exception as exc:
            consecutive_network_failures += 1
            print(
                f"[runner] heartbeat error ({consecutive_network_failures} consecutive): {exc}",
                flush=True,
            )
            _write_status_json(
                runner_id=hostname,
                last_heartbeat_status=f"error: {exc}",
                active_task_ids=[],
                active_compute_job_ids=[],
                caps=caps,
                agent_backend=agent_backend,
                compute_backend=compute_backend,
            )
            if max_network_failures and consecutive_network_failures >= max_network_failures:
                _die(
                    f"heartbeat failed with network error for {consecutive_network_failures} "
                    "consecutive attempts — network appears wedged. Exiting non-zero so "
                    "the operator/supervisor notices instead of looping silently."
                )
        # Sleep in 1s increments so shutdown propagates quickly; add per-runner
        # jitter (±20 % of the 30 s interval) to prevent thundering-herd on restart.
        jitter = random.uniform(-6, 6)
        interval = int(30 + jitter)
        for _ in range(max(1, interval)):
            if _shutdown_event.is_set():
                break
            time.sleep(1)


# ── Compute-job execution ─────────────────────────────────────────────────────

def _derive_registry_host(container_image: str | None) -> str:
    """Derive the registry host from an image reference, or "" for Docker Hub.

    Docker treats the first slash-delimited segment of a reference as a registry
    host ONLY if it looks like one — i.e. it contains a ``.`` (domain) or a ``:``
    (host:port).  Otherwise the reference targets Docker Hub and ``docker login``
    must be invoked with no server argument.

        nvcr.io/nvidia/tensorflow:25.01  -> "nvcr.io"
        myregistry:5000/team/img         -> "myregistry:5000"
        pytorch/pytorch:latest           -> ""   (Docker Hub)
        ubuntu:22.04                     -> ""   (Docker Hub; the ':' is a tag)
    """
    if not container_image:
        return ""
    # A registry host only exists when there is a '/' separating it from the
    # image path.  Without a '/', a bare ``name:tag`` (e.g. ``ubuntu:22.04``) is a
    # Docker Hub image and the ':' is a TAG, not a host:port.
    head, sep, _rest = container_image.partition("/")
    if not sep:
        return ""
    if "." in head or ":" in head:
        return head
    return ""


def _docker_login(auth: dict, container_image: str | None = None, *, _subprocess_run=None) -> bool:
    """Run `docker login` using the credentials in *auth*.

    Returns True on success, False on any failure (non-zero exit, missing
    required fields, subprocess error).  Auth must contain at least a
    ``password`` key; ``server`` and ``username`` are optional but should
    normally be present for private registries.

    When ``server`` is absent/empty the registry host is DERIVED from
    *container_image* (e.g. an NGC image ``nvcr.io/...`` logs into ``nvcr.io``).
    Without this, an omitted server silently logs into Docker Hub and the NGC
    pull then 401s.  A non-host-looking first segment (plain ``user/repo``) still
    means Docker Hub, so we log in with no server argument as before.

    ``_subprocess_run`` is an optional override for the subprocess.run call,
    provided so tests can inject a fake without monkey-patching the global.
    """
    password = auth.get("password") or ""
    if not password:
        return True  # nothing to log in to

    server = auth.get("server") or ""
    if not server:
        server = _derive_registry_host(container_image)
    username = auth.get("username") or ""
    _run = _subprocess_run if _subprocess_run is not None else subprocess.run

    cmd = ["docker", "login", "--password-stdin"]
    if username:
        cmd += ["--username", username]
    if server:
        cmd.append(server)

    try:
        result = _run(
            cmd,
            input=password.encode(),
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace")[:500]
            print(f"[compute] docker login failed (exit={result.returncode}): {stderr}", flush=True)
            return False
        return True
    except subprocess.TimeoutExpired:
        print("[compute] docker login timed out", flush=True)
        return False
    except Exception as exc:
        print(f"[compute] docker login error: {exc}", flush=True)
        return False


# ── Isolation capability cache + GPU scheduling ───────────────────────────────

_CAPS_CACHE: "iso.Capabilities | None" = None


def _get_capabilities() -> "iso.Capabilities":
    """Detect (and cache) host isolation capabilities once for the process."""
    global _CAPS_CACHE
    if _CAPS_CACHE is None:
        _CAPS_CACHE = iso.detect_capabilities()
    return _CAPS_CACHE


def _make_gpu_state(caps: "iso.Capabilities") -> dict:
    """Build the shared GPU-scheduling state (allowlist + in-use set + lock)."""
    allowed = iso.parse_gpu_allowlist(
        os.environ.get("BLUEPRINT_RUNNER_GPU_INDICES"), caps.gpu_indices
    )
    try:
        per_job = max(1, int(os.environ.get("BLUEPRINT_RUNNER_GPUS_PER_JOB", "1")))
    except ValueError:
        per_job = 1
    return {
        "allowed": allowed,
        "in_use": set(),
        "per_job": per_job,
        "lock": threading.Lock(),
    }


def _assign_gpus(gpu_state: dict | None) -> list[int]:
    """Reserve a disjoint GPU subset for a job; [] if none available/configured."""
    if not gpu_state:
        return []
    with gpu_state["lock"]:
        assigned = iso.assign_gpu_indices(
            gpu_state["allowed"], gpu_state["in_use"], gpu_state["per_job"]
        )
        if not assigned:
            return []
        gpu_state["in_use"].update(assigned)
        return assigned


def _release_gpus(gpu_state: dict | None, indices: list[int]) -> None:
    if not gpu_state or not indices:
        return
    with gpu_state["lock"]:
        gpu_state["in_use"].difference_update(indices)


def _read_ldconfig() -> str:
    """Best-effort ``ldconfig -p`` output for locating driver libs outside /usr."""
    try:
        result = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=5
        )
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ── Git clone without the token on argv (GIT_ASKPASS) ─────────────────────────

def _write_git_askpass(dirpath: str) -> str:
    """Write a GIT_ASKPASS helper that reads the token from the environment.

    Keeps the GitHub token OFF argv (it would otherwise be world-readable via
    /proc/cmdline) and out of the persisted .git/config (the clone URL carries no
    credentials). Git invokes the helper with the prompt as $1: 'Username'/
    'Password'. The token is supplied via BLUEPRINT_GIT_TOKEN in the clone env.
    """
    path = os.path.join(dirpath, "git-askpass.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  Username*) printf '%s' 'x-access-token' ;;\n"
            '  *) printf %s "$BLUEPRINT_GIT_TOKEN" ;;\n'
            "esac\n"
        )
    os.chmod(path, stat.S_IRWXU)
    return path


def _clone_repo(
    tmpdir: str,
    clone_dir: str,
    owner: str,
    repo: str,
    ref: str,
    repo_token: str,
) -> tuple[bool, int]:
    """Clone owner/repo@ref into clone_dir. Returns (ok, exit_code).

    The token never appears in argv or config — it is passed via a GIT_ASKPASS
    helper reading BLUEPRINT_GIT_TOKEN from the child env.
    """
    askpass = _write_git_askpass(tmpdir)
    clone_url = f"https://github.com/{owner}/{repo}.git"
    clone_env = {
        "PATH": os.environ.get("PATH", iso._DEFAULT_SANDBOX_PATH),
        "HOME": tmpdir,
        "GIT_ASKPASS": askpass,
        "GIT_TERMINAL_PROMPT": "0",
        "BLUEPRINT_GIT_TOKEN": repo_token,
    }
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", ref, clone_url, clone_dir],
            capture_output=True,
            timeout=300,
            env=clone_env,
        )
    except subprocess.TimeoutExpired:
        return False, 124
    except Exception:
        return False, 1
    return (result.returncode == 0, result.returncode)


def _report_compute_job(
    orchestrator_url: str,
    token: str,
    job_id: str,
    *,
    exec_status: str,
    exit_code: int | None = None,
    result_metrics: dict | None = None,
    command_log: dict | None = None,
) -> bool:
    """POST a status report for a compute job. Returns cancel_requested.

    Terminal reports (``succeeded``/``failed``) carry the measured
    ``result_metrics`` and the job's final status, so a single transient network
    failure on this POST would silently lose the metric and spuriously mark the
    criterion unmeasured. Terminal reports therefore retry transient failures
    (network errors, 429, and 5xx) with capped exponential backoff; interim
    ``running`` heartbeats are sent once, since the next heartbeat re-sends them.
    """
    url = f"{orchestrator_url}/api/runner/compute-job/{job_id}/report"
    body: dict = {"exec_status": exec_status}
    if exit_code is not None:
        body["exit_code"] = exit_code
    if result_metrics is not None:
        body["result_metrics"] = result_metrics
    if command_log is not None:
        body["command_log"] = command_log

    durable = exec_status in _REPORT_TERMINAL_STATUSES
    max_attempts = (_REPORT_MAX_RETRIES + 1) if durable else 1
    for attempt in range(max_attempts):
        last = attempt == max_attempts - 1
        try:
            status, resp = _post(url, token, body)
            if status == 404:
                return True  # job gone, treat as cancel
            # 2xx (accepted) or a permanent 4xx, or no retries left: settle now.
            if 200 <= status < 300 or last or not (status == 429 or status >= 500):
                if isinstance(resp, dict):
                    return bool(resp.get("cancel_requested", False))
                return False
            print(
                f"[compute] report HTTP {status} for job {job_id}; "
                f"retry {attempt + 1}/{_REPORT_MAX_RETRIES}",
                flush=True,
            )
        except Exception as exc:
            if last:
                print(f"[compute] report error for job {job_id}: {exc}", flush=True)
                return False
            print(
                f"[compute] report error for job {job_id}: {exc}; "
                f"retry {attempt + 1}/{_REPORT_MAX_RETRIES}",
                flush=True,
            )
        time.sleep(_report_retry_sleep(attempt))
    return False


def _build_multipart_body(
    *,
    fields: dict,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    file_content_type: str = "application/octet-stream",
) -> tuple[bytes, str]:
    """Build a multipart/form-data body. Returns (body_bytes, content_type).

    Hand-rolled (urllib has no multipart helper) so the runner stays
    dependency-free.  The boundary is randomized so it cannot collide with the
    binary archive payload.
    """
    boundary = "----blueprintrunner" + os.urandom(16).hex()
    bnd = boundary.encode("latin-1")
    crlf = b"\r\n"
    body = bytearray()
    for name, value in fields.items():
        body += b"--" + bnd + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += str(value).encode() + crlf
    body += b"--" + bnd + crlf
    body += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode()
        + crlf
    )
    body += f"Content-Type: {file_content_type}".encode() + crlf + crlf
    body += file_bytes + crlf
    body += b"--" + bnd + b"--" + crlf
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _upload_artifact(orchestrator_url: str, token: str, job_id: str, artifact_dir: str) -> None:
    """Best-effort upload of a run's durable artifacts to the orchestrator.

    Tarballs (gzip) the contents of *artifact_dir* and POSTs it to
    ``/api/runner/compute-job/{job_id}/artifact`` as multipart/form-data (file
    field ``archive`` + form fields ``file_count`` and ``total_bytes``).  Skips
    silently when the dir is empty, and skips (without failing the job) when the
    uncompressed total exceeds the 25 MiB v1 cap.

    ALL failures are non-fatal: the metric report has already been sent by the
    time this runs, so a failed upload must never fail the job.
    """
    try:
        if not os.path.isdir(artifact_dir):
            return

        file_paths: list[str] = []
        for root, _dirs, names in os.walk(artifact_dir):
            for name in names:
                path = os.path.join(root, name)
                if os.path.isfile(path):
                    file_paths.append(path)
        if not file_paths:
            return  # nothing produced — nothing to upload

        file_count = len(file_paths)
        total_bytes = 0
        for path in file_paths:
            try:
                total_bytes += os.path.getsize(path)
            except OSError:
                pass

        cap_mib = _RUNNER_ARTIFACT_MAX_BYTES // (1024 * 1024)
        if total_bytes > _RUNNER_ARTIFACT_MAX_BYTES:
            print(
                f"[compute] artifact {total_bytes / (1024 * 1024):.1f} MiB exceeds "
                f"{cap_mib} MiB v1 upload cap, skipping upload for job {job_id}",
                flush=True,
            )
            return

        # Build the gzip tarball in memory (paths relative to the artifact root).
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(artifact_dir, arcname=".")
        archive_bytes = buf.getvalue()

        body, content_type = _build_multipart_body(
            fields={"file_count": file_count, "total_bytes": total_bytes},
            file_field="archive",
            filename="artifacts.tar.gz",
            file_bytes=archive_bytes,
            file_content_type="application/gzip",
        )
        url = f"{orchestrator_url}/api/runner/compute-job/{job_id}/artifact"
        # Retry transient failures (network errors, 5xx) with the same capped
        # backoff as the terminal report — a blip here otherwise drops the run's
        # debugging artifacts even though the job succeeded.
        for attempt in range(_REPORT_MAX_RETRIES + 1):
            last = attempt == _REPORT_MAX_RETRIES
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    if resp.status in (200, 201, 204):
                        print(
                            f"[compute] uploaded {file_count} artifact file(s) "
                            f"({total_bytes} bytes) for job {job_id}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[compute] artifact upload warning: HTTP {resp.status} for job {job_id}",
                            flush=True,
                        )
                return
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 and not last:
                    print(
                        f"[compute] artifact upload HTTP {exc.code} for job {job_id}; "
                        f"retry {attempt + 1}/{_REPORT_MAX_RETRIES}",
                        flush=True,
                    )
                    time.sleep(_report_retry_sleep(attempt))
                    continue
                print(
                    f"[compute] artifact upload failed (HTTP {exc.code}) for job {job_id}",
                    flush=True,
                )
                return
            except Exception as exc:
                if not last:
                    print(
                        f"[compute] artifact upload network error for job {job_id}: {exc}; "
                        f"retry {attempt + 1}/{_REPORT_MAX_RETRIES}",
                        flush=True,
                    )
                    time.sleep(_report_retry_sleep(attempt))
                    continue
                print(
                    f"[compute] artifact upload network error for job {job_id}: {exc}",
                    flush=True,
                )
                return
    except Exception as exc:  # noqa: BLE001 — upload is strictly best-effort
        print(f"[compute] artifact upload unexpected error for job {job_id}: {exc}", flush=True)


def _parse_result_metrics(result_file: str) -> dict | None:
    """Read numeric metrics from a job's result.json (best-effort, tolerant)."""
    try:
        with open(result_file, encoding="utf-8") as fh:
            parsed = json.loads(fh.read())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    filtered = {
        k: v
        for k, v in parsed.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    return filtered or None


def _run_compute_job(
    job_body: dict,
    orchestrator_url: str,
    token: str,
    active_jobs: set,
    lock: threading.Lock,
    capacity: threading.Semaphore | None = None,
    *,
    capabilities: "iso.Capabilities | None" = None,
    gpu_state: dict | None = None,
    telemetry: "_TelemetryState | None" = None,
) -> None:
    """Worker thread: execute a claimed compute job INSIDE an isolation boundary.

    FAIL-CLOSED. The job runs under a container backend (docker/podman/apptainer)
    or a rootless bwrap sandbox, chosen by ``iso.decide_compute_backend``. If no
    boundary can be built for this job it is DECLINED (reported failed) — the
    command is NEVER executed bare on the host. ``setup_step`` is folded into the
    sandboxed command so it, too, runs inside the boundary (not bare as before).
    """
    job_id = job_body.get("job_id", "unknown")
    command = job_body.get("command", "")
    env_override = dict(job_body.get("env") or {})
    cwd = job_body.get("cwd") or None
    setup_step = job_body.get("setup_step") or None
    container_image = job_body.get("container_image") or None
    container_registry_auth = dict(job_body.get("container_registry_auth") or {})
    runtime_deadline_seconds = job_body.get("runtime_deadline_seconds")
    cancel_requested = bool(job_body.get("cancel_requested", False))
    repo_clone = job_body.get("repo_clone") or None
    repo_token = job_body.get("repo_token") or None
    accelerator_type = job_body.get("accelerator_type") or None
    allow_host_fallback = bool(job_body.get("allow_host_fallback", False))

    caps = capabilities if capabilities is not None else _get_capabilities()
    cpu_limit = os.environ.get("BLUEPRINT_RUNNER_CPU_LIMIT") or None
    mem_limit = os.environ.get("BLUEPRINT_RUNNER_MEM_LIMIT") or None
    has_image = bool(container_image)

    tmpdir: str | None = None
    artifact_dir: str | None = None
    assigned_gpus: list[int] = []
    plan = None
    proc = None
    image_downgraded = False
    # Health-telemetry outcome for this job: "success"/"failure" feed the runner's
    # consecutive-failure counter; None (cancel, graceful shutdown) is neutral —
    # operator-initiated stops are not a runner-health signal. Recorded in finally.
    outcome: str | None = None
    try:
        # ── Fail-closed gate: refuse anything we cannot sandbox, before any work.
        if iso.decide_compute_backend(caps, has_image, allow_host_fallback=allow_host_fallback) == iso.DECLINE:
            reason = iso.compute_decline_reason(caps, has_image, allow_host_fallback=allow_host_fallback) or "no isolation boundary"
            print(f"[compute] DECLINED job {job_id} (fail-closed): {reason}", flush=True)
            outcome = "failure"
            _report_compute_job(
                orchestrator_url, token, job_id,
                exec_status="failed", exit_code=1,
                command_log={
                    "text": f"Runner declined job (fail-closed isolation): {reason}",
                    "truncated": False,
                },
            )
            return

        if cancel_requested:
            _report_compute_job(orchestrator_url, token, job_id,
                                 exec_status="failed", exit_code=1)
            return

        tmpdir = tempfile.mkdtemp(prefix="bp_compute_")
        result_file = os.path.join(tmpdir, "result.json")
        artifact_dir = os.path.join(tmpdir, "artifacts")
        os.makedirs(artifact_dir, exist_ok=True)

        # Reserve a disjoint GPU subset when the host has GPUs (16 GB V100 → 1/job).
        if caps.has_gpu:
            assigned_gpus = _assign_gpus(gpu_state)
            if not assigned_gpus:
                print(
                    f"[compute] job {job_id}: no free GPU to assign; running without "
                    "a fresh device",
                    flush=True,
                )

        # Clone the project repo — token kept OFF argv/config via GIT_ASKPASS.
        clone_dir: str | None = None
        if repo_clone and repo_token:
            owner = str(repo_clone.get("owner") or "")
            repo = str(repo_clone.get("repo") or "")
            ref = str(repo_clone.get("ref") or "main")
            clone_dir = os.path.join(tmpdir, "repo")
            ok, clone_rc = _clone_repo(tmpdir, clone_dir, owner, repo, ref, repo_token)
            if not ok:
                print(f"[compute] git clone failed for job {job_id} (exit={clone_rc})", flush=True)
                outcome = "failure"
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=clone_rc)
                return
            if not cwd:
                cwd = clone_dir
            print(f"[compute] cloned {owner}/{repo}@{ref} for job {job_id}", flush=True)

        # Registry auth for docker/podman backends only (trusted host `docker login`).
        backend_pre = iso.decide_compute_backend(caps, has_image, allow_host_fallback=allow_host_fallback)
        if (
            backend_pre in (iso.BACKEND_DOCKER, iso.BACKEND_PODMAN)
            and container_registry_auth.get("password")
            and not _docker_login(container_registry_auth, container_image=container_image)
        ):
            outcome = "failure"
            _report_compute_job(orchestrator_url, token, job_id,
                                 exec_status="failed", exit_code=1)
            return

        # Fold setup_step into the sandboxed command so it runs INSIDE the same
        # boundary (never bare) and shares its environment. `&&` between brace
        # groups preserves the original semantics: a setup failure aborts before
        # the main command and the job fails with the setup exit code.
        effective_command = command
        if setup_step:
            effective_command = "{\n" + setup_step + "\n} && {\n" + command + "\n}"

        container_name = f"bp-{str(job_id)[:12]}"
        plan = iso.plan_compute_execution(
            caps=caps,
            command=effective_command,
            image=container_image,
            registry_auth=container_registry_auth or None,
            workspace_host=tmpdir,
            result_file_host=result_file,
            artifact_dir_host=artifact_dir,
            clone_dir_host=clone_dir,
            cwd_host=cwd,
            job_env=env_override,
            gpu_indices=assigned_gpus,
            container_name=container_name,
            source_env=dict(os.environ),
            # ldconfig is only consulted for the bwrap backend (to bind driver
            # libs living outside /usr); container backends provide their own.
            ldconfig_output=(
                _read_ldconfig()
                if (assigned_gpus and backend_pre == iso.BACKEND_BWRAP)
                else ""
            ),
            cpu_limit=cpu_limit,
            mem_limit=mem_limit,
            allow_host_fallback=allow_host_fallback,
        )
        if plan.declined:
            print(f"[compute] DECLINED job {job_id} (fail-closed): {plan.decline_reason}", flush=True)
            outcome = "failure"
            _report_compute_job(
                orchestrator_url, token, job_id,
                exec_status="failed", exit_code=1,
                command_log={
                    "text": f"Runner declined job (fail-closed isolation): {plan.decline_reason}",
                    "truncated": False,
                },
            )
            return

        # GPU-used enforcement: when the job declares an accelerator requirement,
        # run a lightweight nvidia-smi -L probe INSIDE the same sandbox before
        # the real command so torch/TF cannot silently fall back to CPU if CUDA
        # is not actually reachable inside the sandbox.
        if accelerator_type and assigned_gpus:
            probe_ok, probe_detail = iso.probe_gpu_in_sandbox(plan)
            if not probe_ok:
                terminal_reason = (
                    f"GPU declared (accelerator={accelerator_type}) but unusable "
                    f"in sandbox: {probe_detail}"
                )
                print(f"[compute] ABORT job {job_id}: {terminal_reason}", flush=True)
                outcome = "failure"
                _report_compute_job(
                    orchestrator_url, token, job_id,
                    exec_status="failed", exit_code=1,
                    command_log={
                        "text": terminal_reason,
                        "truncated": False,
                    },
                )
                return
            print(
                f"[compute] GPU probe OK for job {job_id}: {probe_detail.splitlines()[0]}",
                flush=True,
            )

        # Per-job image downgrade: the job asked for a container_image but this host
        # has no container runtime, so it runs image-LESS under bwrap on the host
        # toolchain. Record it so a resulting failure (missing CUDA/lib the image
        # would have provided) is diagnosable from the job's OWN command_log rather
        # than a mystery. (Host/preflight only labels this at the runner level.)
        image_downgraded = has_image and plan.backend == iso.BACKEND_BWRAP
        if image_downgraded:
            print(
                f"[compute] job {job_id}: container_image {container_image!r} requested but no "
                "container runtime — running image-less under bwrap on host toolchain.",
                flush=True,
            )

        # Optional best-effort resource scope (systemd-run --user --scope) for the
        # NON-daemon backends (bwrap/apptainer). Container backends already carry
        # --cpus/--memory in their argv, so a scope would double-apply. When
        # systemd-run is unavailable, fall back to an RLIMIT_AS preexec for memory;
        # if neither is available the cap is simply not applied (never fail-closed).
        scope_prefix: list = []
        if plan.backend in (iso.BACKEND_BWRAP, iso.BACKEND_APPTAINER, iso.BACKEND_ENROOT):
            scope_prefix = iso.build_resource_scope_prefix(
                caps, cpu_limit=cpu_limit, mem_limit=mem_limit, scope_name=container_name
            )
        run_argv = [*scope_prefix, *plan.argv]
        preexec = None
        if not scope_prefix and mem_limit:
            preexec = iso.rlimit_preexec(mem_limit)

        popen_kwargs: dict = {
            "env": plan.env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if hasattr(os, "setsid"):
            popen_kwargs["start_new_session"] = True
        if preexec is not None:
            popen_kwargs["preexec_fn"] = preexec
        try:
            proc = subprocess.Popen(run_argv, **popen_kwargs)
        except Exception as exc:
            print(f"[compute] failed to start job {job_id}: {exc}", flush=True)
            outcome = "failure"
            _report_compute_job(orchestrator_url, token, job_id,
                                 exec_status="failed", exit_code=1)
            return

        print(
            f"[compute] started job {job_id} pid={proc.pid} backend={plan.backend} "
            f"gpus={assigned_gpus}",
            flush=True,
        )
        _report_compute_job(orchestrator_url, token, job_id, exec_status="running")

        # Capture stdout with dedup + head/tail budget (see _BoundedDedupLog).
        log_capture = _BoundedDedupLog(
            _RUNNER_MAX_COMMAND_LOG_BYTES, _RUNNER_COMMAND_LOG_HEAD_BYTES
        )

        def _read_stdout() -> None:
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    log_capture.feed(chunk)
            except (ValueError, OSError):
                # stdout was closed underneath us (teardown / fd-leak guard) — the
                # blocked read() returns/raises; stop quietly rather than spewing a
                # traceback from a daemon thread.
                return

        def _close_stdout() -> None:
            # Force the reader thread's blocked read() to EOF and release the fd.
            # Killing the process is NOT sufficient when the job backgrounds a child
            # that inherits (and keeps open) stdout — the pipe write-end stays open,
            # read() never returns, and both the reader thread and the fd leak.
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()

        # Monotonic clock for all deadline math: a wall-clock jump (NTP step, laptop
        # resume) must not prematurely time out — or indefinitely extend — a job.
        start_time = time.monotonic()
        deadline = (
            start_time + float(runtime_deadline_seconds)
            if runtime_deadline_seconds is not None
            else None
        )

        def _teardown() -> None:
            iso.terminate_process_tree(
                proc,
                backend=plan.backend,
                container_name=plan.container_name,
                runtime_path=plan.backend,
            )
            _close_stdout()  # release reader thread + fd even if a child holds stdout
            reader.join(timeout=2)

        exit_code_val: int | None = None
        while True:
            try:
                exit_code_val = proc.wait(timeout=_RUNNER_HEARTBEAT_INTERVAL)
                break  # process finished
            except subprocess.TimeoutExpired:
                pass

            now = time.monotonic()
            if deadline is not None and now >= deadline:
                _teardown()
                outcome = "failure"
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=124)
                return

            if _shutdown_event.is_set():
                _teardown()
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=1)
                return

            cancel = _report_compute_job(orchestrator_url, token, job_id,
                                          exec_status="running")
            if cancel:
                _teardown()
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=1)
                return

        # Process exited: let the reader drain, then close our read-fd so a leftover
        # reader (backgrounded child still holding the pipe) is unblocked and the fd
        # is never leaked across the runner's lifetime.
        reader.join(timeout=5)
        _close_stdout()
        reader.join(timeout=1)

        result_metrics = _parse_result_metrics(result_file)

        log_text, truncated = log_capture.result()
        if image_downgraded:
            note = (
                f"NOTE: container_image {container_image} requested but no container "
                "runtime available on this host; running image-less on host toolchain.\n"
            )
            log_text = note + log_text
        command_log: dict | None = None
        if log_text or truncated or image_downgraded:
            command_log = {
                "text": log_text,
                "truncated": truncated,
                "byte_count": len(log_text.encode("utf-8")),
            }

        succeeded = exit_code_val == 0
        outcome = "success" if succeeded else "failure"
        exec_status_final = "succeeded" if succeeded else "failed"
        _report_compute_job(
            orchestrator_url, token, job_id,
            exec_status=exec_status_final,
            exit_code=exit_code_val,
            result_metrics=result_metrics,
            command_log=command_log,
        )
        print(f"[compute] job {job_id} {exec_status_final} (exit={exit_code_val})",
              flush=True)

    except Exception as exc:
        print(f"[compute] unexpected error for job {job_id}: {exc}", flush=True)
        outcome = "failure"
        try:
            _report_compute_job(orchestrator_url, token, job_id,
                                 exec_status="failed", exit_code=1)
        except Exception:
            pass
    finally:
        # Record health telemetry for this job (neutral outcomes leave it untouched).
        if telemetry is not None:
            if outcome == "success":
                telemetry.record_success()
            elif outcome == "failure":
                telemetry.record_failure()
        # Upload durable artifacts BEFORE tearing down tmpdir (every terminal path).
        if artifact_dir:
            _upload_artifact(orchestrator_url, token, job_id, artifact_dir)
        _release_gpus(gpu_state, assigned_gpus)
        with lock:
            active_jobs.discard(job_id)
        _release_capacity(capacity)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        # Cache hygiene: reclaim Docker image storage if disk is low. No-op when
        # free space is above threshold; protects in-use and recent images.
        try:
            prune_result = _prune_docker_images(
                _workspace_root(), _min_free_disk_bytes()
            )
            if prune_result.get("pruned"):
                print(
                    f"[compute] job {job_id}: pruned Docker images (low disk) "
                    f"keep_days={prune_result.get('keep_days')}",
                    flush=True,
                )
        except Exception:  # noqa: BLE001 — cache hygiene must never fail a job
            pass


def _compute_poll_loop(
    orchestrator_url: str,
    token: str,
    active_jobs: set,
    lock: threading.Lock,
    max_concurrent: int,
    capacity: threading.Semaphore | None = None,
    base_poll_interval: float = 2.0,
    capabilities: "iso.Capabilities | None" = None,
    gpu_state: dict | None = None,
    telemetry: "_TelemetryState | None" = None,
    max_gpu_util_pct: int = 0,
) -> None:
    """Background thread: poll for compute jobs and spawn a worker per claimed job.

    The loop body is wrapped so a transient error never silently kills the poll
    thread (which would strand compute capacity); the ``main()`` supervisor makes
    an actually-dead thread fatal.
    """
    url = f"{orchestrator_url}/api/runner/next-compute-job"
    poll_interval = base_poll_interval

    while not _shutdown_event.is_set():
        try:
            with lock:
                active_count = len(active_jobs)

            if active_count >= max_concurrent:
                time.sleep(poll_interval)
                continue

            if not _try_acquire_capacity(capacity):
                time.sleep(poll_interval)
                continue

            # Host-load guard: skip claiming when GPU is saturated.
            if max_gpu_util_pct > 0:
                util = _gpu_utilization_pct()
                if util is not None and util >= max_gpu_util_pct:
                    _release_capacity(capacity)
                    print(
                        f"[compute] gpu_utilization_pct {util} >= threshold {max_gpu_util_pct}"
                        " — skipping compute claim",
                        flush=True,
                    )
                    time.sleep(poll_interval)
                    continue

            try:
                resp = _post(url, token, {}, return_headers=True)
                if isinstance(resp, tuple) and len(resp) == 3:
                    status, body, retry_after = resp
                else:  # test fakes / callers that return the 2-tuple form
                    status, body = resp
                    retry_after = None
            except Exception as exc:
                _release_capacity(capacity)
                print(f"[compute] poll error: {exc}", flush=True)
                time.sleep(5)
                continue

            if status == 429:
                # Rate limited: honor the server's Retry-After instead of a fixed
                # sleep so we back off exactly as long as asked (and no longer).
                wait = retry_after if retry_after is not None else min(poll_interval * 2, 30.0)
                wait = max(0.0, min(wait, 300.0))
                print(f"[compute] poll 429 — backing off {wait:.1f}s (Retry-After)", flush=True)
                _release_capacity(capacity)
                time.sleep(wait)
                continue

            if status == 401:
                # A 401 is NOT fatal: the token is still valid, but the runner row may
                # have been deactivated after a missed heartbeat (slept/network-dropped
                # laptop).  The background heartbeat re-arms the row (status->active),
                # so we just back off and keep polling instead of exiting.
                print(
                    "[compute] poll 401 — runner token rejected; backing off and retrying "
                    "(row may be reactivating via heartbeat).",
                    flush=True,
                )
                _release_capacity(capacity)
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status == 204 or body is None:
                _release_capacity(capacity)
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status != 200:
                _release_capacity(capacity)
                print(f"[compute] poll warning: HTTP {status}", flush=True)
                time.sleep(5)
                continue

            poll_interval = base_poll_interval

            job_id = str(body.get("job_id", "unknown"))
            with lock:
                if job_id in active_jobs:
                    print(f"[compute] skipping duplicate job {job_id}", flush=True)
                    _release_capacity(capacity)
                    time.sleep(poll_interval)
                    continue
                active_jobs.add(job_id)

            print(f"[compute] claimed job {job_id}", flush=True)
            try:
                worker = threading.Thread(
                    target=_run_compute_job,
                    args=(body, orchestrator_url, token, active_jobs, lock, capacity),
                    kwargs={
                        "capabilities": capabilities,
                        "gpu_state": gpu_state,
                        "telemetry": telemetry,
                    },
                    daemon=True,
                )
                worker.start()
            except Exception as exc:
                with lock:
                    active_jobs.discard(job_id)
                _release_capacity(capacity)
                print(f"[compute] failed to start worker for job {job_id}: {exc}", flush=True)
                try:
                    _report_compute_job(
                        orchestrator_url,
                        token,
                        job_id,
                        exec_status="failed",
                        exit_code=1,
                        command_log=f"Runner failed to start compute worker: {exc}",
                    )
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 — keep the poll thread alive
            print(f"[compute] poll loop error (continuing): {exc}", flush=True)
            time.sleep(5)


# ── Preflight + main ──────────────────────────────────────────────────────────

def _workspace_root() -> str:
    root = os.environ.get("BLUEPRINT_RUNNER_WORKSPACE_ROOT", "").strip()
    if not root:
        root = os.path.join(tempfile.gettempdir(), "blueprint-agent-workspaces")
    return os.path.abspath(root)


def _workspace_writable(root: str) -> bool:
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".bp_write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _cleanup_stale_workspaces(active_task_ids: set, max_age_seconds: float = 300) -> int:
    """Remove per-task workspace dirs that are not in *active_task_ids* and older than *max_age_seconds*."""
    root = _workspace_root()
    if not os.path.isdir(root):
        return 0
    active_dirs: set[str] = set()
    for tid in active_task_ids:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tid)
        if safe:
            active_dirs.add(safe)
    now = time.time()
    removed = 0
    try:
        for name in os.listdir(root):
            if name in active_dirs:
                continue
            dirpath = os.path.join(root, name)
            if not os.path.isdir(dirpath):
                continue
            try:
                if (now - os.stat(dirpath).st_mtime) >= max_age_seconds:
                    shutil.rmtree(dirpath, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


# ── Per-task log file persistence ─────────────────────────────────────────────

# Default max age for task log files before cleanup (7 days).
_MAX_TASK_LOG_AGE_SECONDS = 7 * 24 * 3600  # 7 days
# Max number of task log files to retain (safety cap).
_MAX_TASK_LOG_FILES = 200


def _task_log_dir() -> str:
    """Directory for per-task log files.

    Defaults to ``<credential_dir>/task-logs/`` so logs live alongside the
    runner's credential/status files. Override via ``BLUEPRINT_RUNNER_LOG_DIR``
    for testing or non-standard installs.
    """
    override = os.environ.get("BLUEPRINT_RUNNER_LOG_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(_credential_dir(), "task-logs")


def _task_log_path(task_id: str) -> str:
    """Return the per-task log file path for *task_id*.

    The filename is the sanitized task ID with a ``.log`` extension, stored
    inside the task log directory (``_task_log_dir()``).
    """
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)
    if not safe_id:
        safe_id = "unknown"
    return os.path.join(_task_log_dir(), f"{safe_id}.log")


def _cleanup_stale_task_logs(
    max_age_seconds: float = _MAX_TASK_LOG_AGE_SECONDS,
    max_files: int = _MAX_TASK_LOG_FILES,
) -> int:
    """Remove stale task log files to prevent unbounded disk growth.

    Two-phase cleanup:
      1. Remove all ``.log`` files in the task log directory whose mtime is
         older than *max_age_seconds*.
      2. After age-based removal, if the file count still exceeds *max_files*,
         remove the oldest files until within the limit.

    Returns the number of files removed. Best-effort — never raises.
    """
    log_dir = _task_log_dir()
    if not os.path.isdir(log_dir):
        return 0
    now = time.time()
    removed = 0
    # Collect all .log files with their mtime (oldest first).
    entries: list[tuple[float, str]] = []
    try:
        for name in os.listdir(log_dir):
            if not name.endswith(".log"):
                continue
            path = os.path.join(log_dir, name)
            try:
                st = os.stat(path)
                if not stat.S_ISREG(st.st_mode):
                    continue
                entries.append((st.st_mtime, path))
            except OSError:
                continue
    except OSError:
        return 0
    if not entries:
        return 0
    entries.sort(key=lambda x: x[0])

    # Phase 1: remove files older than max_age_seconds.
    surviving: list[tuple[float, str]] = []
    for mtime, path in entries:
        if (now - mtime) >= max_age_seconds:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                surviving.append((mtime, path))
        else:
            surviving.append((mtime, path))

    # Phase 2: if still over the file cap, remove oldest.
    while len(surviving) > max_files:
        _mtime, path = surviving.pop(0)
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def _compute_backend_summary(caps: "iso.Capabilities") -> str:
    """Human summary of which compute backends this host can build."""
    parts: list[str] = []
    container = caps.preferred_container()
    if container:
        parts.append(f"{container} (image jobs, --nv/--gpus)")
    if caps.bwrap_ok:
        parts.append("bwrap (imageless + image-downgrade)")
    return ", ".join(parts) if parts else "none"


# Provider credential env keys checked in --check preflight.
# The agent task needs at least one provider credential to call an LLM. A green
# --check with none set means every claimed agent task will immediately fail at
# PRE_CLAIM_FAILED (no LLM provider to call). This is surfaced as a non-blocking
# warning — the operator may add credentials after --check or use a provider path
# not captured here.
_PROVIDER_CREDENTIAL_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ZAI_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def _check_provider_credentials() -> list[str]:
    """Return env-var names of provider credentials that are set (non-empty value).

    An agent task needs at least one provider credential to call an LLM. When none
    are found the --check preflight warns so the operator catches the misconfiguration
    before dispatching work.
    """
    found: list[str] = []
    for key in _PROVIDER_CREDENTIAL_KEYS:
        if os.environ.get(key, "").strip():
            found.append(key)
    return found


def _agent_deps_missing() -> bool:
    """Check whether agent node dependencies are missing on this host.

    Returns True when ``agent/src/index.ts`` exists on this host (agent path
    is expected) but ``agent/node_modules/.bin/tsx`` is absent AND ``tsx`` is
    not on the system PATH — meaning ``npm install`` / ``npm ci`` was never
    run in the agent directory. The tsx fallback path in ``_locate_agent_binary``
    returns a non-None argv (``["tsx", entry_ts]``) even in this state, so the
    runner appears ready while every agent task fails at subprocess spawn.

    Returns False when no agent source is found (compute-only host — not
    actionable) or when deps are properly installed.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in [os.path.join(script_dir, ".."), os.getcwd()]:
        agent_dir = os.path.normpath(os.path.join(base, "agent"))
        entry_ts = os.path.join(agent_dir, "src", "index.ts")
        if os.path.isfile(entry_ts):
            # Agent source exists on this host — check deps.
            tsx_in_node_modules = os.path.isfile(
                os.path.join(agent_dir, "node_modules", ".bin", "tsx")
            )
            # dist/index.js means a pre-built artifact and no tsx needed.
            dist_built = os.path.isfile(
                os.path.join(agent_dir, "dist", "index.js")
            )
            if not tsx_in_node_modules and not dist_built and not shutil.which("tsx"):
                return True
            # Deps are fine — no warning needed.
            return False
    # No agent source path on this host at all — compute-only, not actionable.
    return False


def _preflight(
    orchestrator_url: str,
    token: str,
    agent_binary: list[str] | None,
    caps: "iso.Capabilities",
    gpu_state: dict,
    max_concurrent: int,
    max_concurrent_compute: int,
    agent_image: str | None = None,
) -> dict:
    """Print the aligned preflight block; return a dict of the decisions.

    Performs the single authoritative orchestrator/token ping (a real heartbeat)
    so ``--check`` and normal boot share one code path.
    """
    # Use the same fail-safe helper as the heartbeat loop: a bare
    # socket.gethostname() can raise OSError and would crash boot/--check.
    hostname = _hostname()
    token_prefix = token[:8] + "..." if len(token) >= 8 else token
    workspace_root = _workspace_root()
    ws_writable = _workspace_writable(workspace_root)

    gpu_indices_str = ",".join(str(i) for i in caps.gpu_indices) or "none"
    allowlist = gpu_state.get("allowed") or []
    allowlist_str = ",".join(str(i) for i in allowlist) or "none"

    claimable = iso.claimable_classes(caps, agent_binary is not None)
    # A docker+image box claims (read-only) agent work even without bwrap; the
    # host agent binary is carried by the image, so claimable_classes (which only
    # knows about the bwrap path) under-reports it. Relax it here.
    bwrap_agent = claimable[iso.CLASS_AGENT]
    docker_agent = _docker_agent_possible(caps, agent_image)
    if docker_agent:
        claimable = {**claimable, iso.CLASS_AGENT: True}
    # Runner-DECLARED agent capability floor (mirrors the heartbeat loop): a
    # docker-ONLY box (docker+image, no bwrap agent path) can run only the read-only
    # agent slice, so it declares that slice for the orchestrator to intersect with
    # the operator policy. bwrap-capable / no-agent boxes declare None (no restriction).
    supported_agent_task_types = (
        sorted(iso.AGENT_DOCKER_TYPES)
        if (docker_agent and not bwrap_agent)
        else None
    )
    agent_reason = iso.agent_decline_reason(caps, agent_binary is not None)
    compute_reason = None
    if not claimable[iso.CLASS_COMPUTE]:
        compute_reason = "no bwrap sandbox and no usable container runtime"

    gpu_caps = _detect_gpu()

    # Orchestrator / token ping (the token validation).
    orchestrator_ok = False
    token_valid = False
    token_401 = False
    ping_error: str | None = None
    enrolled_max_concurrent: int | None = None
    try:
        status, ping_body = _post(
            f"{orchestrator_url}/api/runner/heartbeat",
            token,
            {
                "active_task_ids": [],
                "active_compute_job_ids": [],
                "agent_available": claimable[iso.CLASS_AGENT],
                "supported_agent_task_types": supported_agent_task_types,
                "hostname": hostname,
                "version": _runner_version(),
                # Isolation-backend inventory + selected backends so `--check`
                # populates the fleet view the same way the running heartbeat does.
                "isolation_backends": iso.capabilities_inventory(caps),
                "agent_backend": iso.decide_agent_backend(
                    caps, agent_binary_present=agent_binary is not None,
                    agent_image=agent_image,
                ),
                "compute_backend": iso.decide_compute_backend(
                    caps, has_image=agent_image is not None
                ),
                **gpu_caps,
            },
        )
        orchestrator_ok = True
        if status in (200, 204):
            token_valid = True
            if isinstance(ping_body, dict):
                raw_emc = ping_body.get("enrolled_max_concurrent")
                if isinstance(raw_emc, int) and raw_emc > 0:
                    enrolled_max_concurrent = raw_emc
        elif status == 401:
            token_401 = True
    except Exception as exc:
        ping_error = str(exc)

    def _fmt(label: str, value: str) -> None:
        print(f"  {label:<16}: {value}", flush=True)

    print("Blueprint local fleet runner — preflight", flush=True)
    _fmt("runner host", hostname or "unknown")
    if ping_error:
        _fmt("orchestrator", f"{orchestrator_url}   [UNREACHABLE: {ping_error}]")
    else:
        _fmt("orchestrator", f"{orchestrator_url}   [reachable]")
    if token_valid:
        token_state = "valid"
    elif token_401:
        token_state = "401 — inactive/stale row (heartbeat re-arms)"
    elif ping_error:
        token_state = "unknown (orchestrator unreachable)"
    else:
        token_state = "rejected"
    _fmt("runner token", f"{token_prefix}   [{token_state}]")
    _fmt("python", sys.version.split()[0])
    _fmt(
        "workspace",
        f"{workspace_root}   [{'writable' if ws_writable else 'NOT WRITABLE'}]",
    )
    _fmt(
        "concurrency",
        f"agents={max_concurrent}  compute={max_concurrent_compute}  "
        f"(separate pools; up to {max_concurrent + max_concurrent_compute} tasks total)",
    )

    print("  isolation boundaries:", flush=True)
    bwrap_state = "not installed"
    if caps.bwrap_path:
        bwrap_state = f"{caps.bwrap_path}  [userns: {'yes' if caps.userns_ok else 'NO'}]  -> {'usable' if caps.bwrap_ok else 'UNUSABLE'}"
    print(f"    bwrap        : {bwrap_state}", flush=True)
    print(
        f"    apptainer    : {caps.apptainer_path or 'not installed'}",
        flush=True,
    )
    if not shutil.which("docker"):
        docker_state = "not installed"
    elif caps.docker_usable:
        docker_state = "usable"
    else:
        docker_state = "present but UNUSABLE (socket denied?)"
    print(f"    docker       : {docker_state}", flush=True)
    print(
        f"    podman       : {'usable' if caps.podman_usable else ('present but unusable' if shutil.which('podman') else 'not installed')}",
        flush=True,
    )
    caps_state = (
        f"{caps.systemd_run_path} (resource caps available)"
        if caps.systemd_run_path
        else "none (resource caps best-effort via rlimit only)"
    )
    print(f"    resource cap : {caps_state}", flush=True)

    _fmt("gpus", f"{len(caps.gpu_indices)}x [{gpu_indices_str}]")
    _fmt(
        "gpu policy",
        f"allowlist=[{allowlist_str}]  per_job={gpu_state.get('per_job')}",
    )

    # GPU sandbox smoke test: verify CUDA is actually reachable inside the bwrap
    # sandbox that GPU compute jobs will use. Runs only when GPUs are detected and
    # bwrap is available; a non-zero exit or empty output here means any
    # accelerator-declared job would be aborted by the GPU probe gate.
    if caps.has_gpu and caps.bwrap_ok and allowlist:
        _smoke_indices = list(allowlist)[:1]
        try:
            import tempfile as _tempfile
            _smoke_tmpdir = _tempfile.mkdtemp(prefix="bp_gpu_smoke_")
            _smoke_result_file = os.path.join(_smoke_tmpdir, "result.json")
            _smoke_artifact_dir = os.path.join(_smoke_tmpdir, "artifacts")
            os.makedirs(_smoke_artifact_dir, exist_ok=True)
            _smoke_ldconfig = _read_ldconfig() if iso.BACKEND_BWRAP else ""
            _smoke_plan = iso.plan_compute_execution(
                caps=caps,
                command="nvidia-smi -L",
                image=None,
                registry_auth=None,
                workspace_host=_smoke_tmpdir,
                result_file_host=_smoke_result_file,
                artifact_dir_host=_smoke_artifact_dir,
                clone_dir_host=None,
                cwd_host=None,
                job_env={},
                gpu_indices=_smoke_indices,
                container_name="bp-smoke",
                source_env=dict(os.environ),
                ldconfig_output=_smoke_ldconfig,
            )
            import shutil as _shutil
            _shutil.rmtree(_smoke_tmpdir, ignore_errors=True)
            if not _smoke_plan.declined:
                _probe_ok, _probe_detail = iso.probe_gpu_in_sandbox(_smoke_plan)
                if _probe_ok:
                    _fmt(
                        "gpu sandbox",
                        f"CUDA reachable: {_probe_detail.splitlines()[0]}",
                    )
                else:
                    _fmt("gpu sandbox", f"WARNING — CUDA unreachable in sandbox: {_probe_detail}")
            else:
                _fmt("gpu sandbox", f"plan declined: {_smoke_plan.decline_reason}")
        except Exception as _exc:
            _fmt("gpu sandbox", f"smoke check error: {_exc}")
    elif caps.has_gpu:
        _fmt("gpu sandbox", "skipped (bwrap not usable)")

    for note in caps.notes:
        print(f"    note         : {note}", flush=True)

    # Provider credential check. Host env credentials are a FALLBACK only: the
    # orchestrator ships per-task pool credentials via the /next-task claim path,
    # so a pool-covered runner can claim and run agent work even with no host env
    # creds. We downgrade this to a WARNING so --check passes green for pool-covered
    # runners. Hard-fail is reserved for when we can positively confirm the
    # orchestrator has no pool accounts at all (not implemented yet — that would
    # require an extra API call; omitted until the pool-empty signal is available).
    provider_creds = _check_provider_credentials()
    agent_claimable = claimable[iso.CLASS_AGENT]
    if provider_creds:
        _fmt("provider creds", f"present ({len(provider_creds)} key(s))")
    elif agent_claimable:
        print(
            "  provider creds  : WARNING — no host LLM credential found.\n"
            "                    Pool dispatch covers claims (per-task creds via\n"
            "                    /next-task); host env is fallback only if the pool\n"
            "                    is unconfigured or exhausted. Set ANTHROPIC_API_KEY,\n"
            "                    CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY, or similar\n"
            "                    to enable the host-credential fallback path.",
            flush=True,
        )
    else:
        print(
            "  provider creds  : NONE (compute-only runner — not required)",
            flush=True,
        )

    print("  task classes:", flush=True)
    if claimable[iso.CLASS_AGENT]:
        if caps.bwrap_ok:
            agent_via = "bwrap" + (f" + docker image {agent_image}" if docker_agent else "")
        else:
            agent_via = f"docker image {agent_image} (read-only types)"
        print(f"    agent        : CLAIMED via {agent_via}", flush=True)
    else:
        print(f"    agent        : DECLINED — {agent_reason}", flush=True)
    if claimable[iso.CLASS_COMPUTE]:
        print(
            f"    compute      : CLAIMED via {_compute_backend_summary(caps)}",
            flush=True,
        )
    else:
        print(f"    compute      : DECLINED — {compute_reason}", flush=True)

    if not (claimable[iso.CLASS_AGENT] or claimable[iso.CLASS_COMPUTE]):
        print(
            "  No isolation boundary available — install bubblewrap (or apptainer) "
            "to enable work.",
            flush=True,
        )

    # Agent deps check: surface an actionable message when agent source exists
    # on this host but node dependencies were never installed (tsx not found).
    # The docker agent backend carries the runtime in the image, so host deps
    # are irrelevant there — only warn for the bwrap path.
    if caps.bwrap_ok and _agent_deps_missing():
        print(
            "    agent deps  : MISSING — run `cd agent && npm install` (or re-run\n"
            "                  `blueprint-runner install`, which now bootstraps them).",
            flush=True,
        )

    # Process-supervisor check. A runner with no supervisor that self-updates
    # will `os._exit`/die and never come back (operator-observed dgx-1 ~45min
    # outage). The auto-updater now re-execs in place when unsupervised, but a
    # crash still needs a restarter — so surface the recommended launch patterns.
    supervisor = None
    try:
        supervisor = _detect_process_supervisor()
    except Exception:
        supervisor = None
    if supervisor is not None:
        _fmt("supervisor", f"{supervisor} (auto-restart on crash/update)")
    else:
        print(
            "  supervisor      : NONE DETECTED\n"
            "                    No process supervisor found. If launched manually\n"
            "                    (tmux, nohup, screen), the runner recovers from\n"
            "                    auto-updates via in-place re-exec, but a CRASH will\n"
            "                    NOT restart it. Wrap it in a restart loop or a\n"
            "                    systemd service. See RUNNER_RUNBOOK (--help).",
            flush=True,
        )

    setup_ok = (
        (token_valid or token_401)
        and ws_writable
        and (claimable[iso.CLASS_AGENT] or claimable[iso.CLASS_COMPUTE])
    )
    return {
        "orchestrator_ok": orchestrator_ok,
        "token_valid": token_valid,
        "token_401": token_401,
        "ping_error": ping_error,
        "claimable": claimable,
        "gpu_caps": gpu_caps,
        "workspace_writable": ws_writable,
        "supervisor": supervisor,
        "setup_ok": setup_ok,
        "enrolled_max_concurrent": enrolled_max_concurrent,
    }


def _safe_credential_path(fpath: str, workspace_root: str) -> str:
    """Validate and resolve a credential file path, rejecting directory traversal.

    The backend supplies ``fpath`` as a sandbox-absolute path (e.g.
    ``/root/.config/claude/settings.json``). Leading ``/`` is stripped
    so the path is relative to *workspace_root* (which is bind-mounted at
    the sandbox root for both bwrap and docker backends).

    Raises ``ValueError`` on any path containing ``..`` segments or whose
    resolved absolute path falls outside *workspace_root* — so a compromised
    or buggy orchestrator cannot write credential files to arbitrary host
    locations via e.g. ``../../.ssh/authorized_keys``.
    """
    # Defense-in-depth: reject '..' path segments before any processing.
    stripped = fpath.lstrip("/")
    for component in stripped.split("/"):
        if component == "..":
            raise ValueError(
                f"credential path contains '..' (directory traversal): {fpath!r}"
            )

    # Resolve relative to workspace root and verify containment.
    full_path = os.path.abspath(os.path.join(workspace_root, stripped))
    ws_abs = os.path.abspath(workspace_root)

    try:
        rel = os.path.relpath(full_path, ws_abs)
    except ValueError:
        raise ValueError(
            f"credential path {fpath!r} resolves outside workspace "
            f"{ws_abs!r} (os.path.relpath failed)"
        )
    if rel.startswith("..") or os.path.isabs(rel):
        raise ValueError(
            f"credential path {fpath!r} resolves to {full_path}, "
            f"which is outside workspace {ws_abs}"
        )

    return full_path


def _spawn_agent_task(
    *,
    task_id: str,
    task_type: str,
    agent_token: str,
    vm_instance_id: str,
    orchestrator_url: str,
    workspace: str,
    caps: "iso.Capabilities",
    agent_binary: list[str] | None,
    agent_image: str | None,
    active_tasks: dict,
    agent_started_at: dict,
    agent_teardown: dict,
    lock: threading.Lock,
    llm_env: dict[str, str] | None = None,
    llm_files: list[dict[str, str]] | None = None,
) -> bool:
    """Plan (fail-closed) + spawn one claimed agent task; record it for reaping.

    Routes the per-task backend choice through ``iso.decide_agent_backend`` with the
    task_type + agent_image so only READ-ONLY task types get the docker backend when
    only a (read-only) image is present. The runner-set identity env
    (AGENT_SECRET_TOKEN / ORCHESTRATOR_URL / TASK_ID / VM_INSTANCE_ID) flows through
    the isolation layer unchanged — into bwrap's env or the docker ``-e`` set.

    On success, records ``(proc, start_time, (backend, container_name))`` into the
    shared maps under ``lock`` and returns True — the CALLER must NOT release the
    capacity permit (the reaper releases it when the task exits). Returns False on
    DECLINE / spawn failure (the caller releases the permit).
    """
    present = agent_binary is not None
    backend = iso.decide_agent_backend(
        caps, present, task_type=task_type, agent_image=agent_image
    )
    if backend == iso.DECLINE:
        # No isolation boundary for THIS task type on this host (fail-closed).
        reason = iso.agent_decline_reason(caps, present) or "no agent sandbox"
        print(
            f"[runner] DECLINED agent task {task_id} (fail-closed): {reason}",
            flush=True,
        )
        return False
    # The bwrap backend needs the HOST agent dist; the docker backend carries the
    # agent in the image, so a missing host binary is only fatal off the docker path.
    if backend != iso.BACKEND_DOCKER and not agent_binary:
        print(
            f"[runner] agent binary unavailable — cannot run agent task {task_id}; "
            "skipping (run `cd agent && npm install` so tsx can start "
            "agent/src/index.ts, or set BLUEPRINT_AGENT_BINARY to override "
            "auto-detection). Compute jobs are unaffected.",
            flush=True,
        )
        return False

    # Only hand the image to the planner when THIS task's decision is docker; a
    # bwrap-eligible task on a docker+image box still takes the bwrap path.
    plan_image = agent_image if backend == iso.BACKEND_DOCKER else None

    # Write backend-supplied credential files into the agent's workspace
    # (bind-mounted as / inside the bwrap sandbox; mounted at /agent-workdir
    #  inside the docker sandbox). The LLM provider CLI (Claude, Codex,
    # Gemini) reads config files from well-known paths;
    # the runner materializes them per-task so the agent finds exactly the
    # credential the pool assigned.
    if llm_files:
        for f in llm_files:
            try:
                fpath = _safe_credential_path(f["path"], workspace)
                fcontent = f["content"]
                fmode = f.get("mode", "0600")
                parent = os.path.dirname(fpath)
                os.makedirs(parent, exist_ok=True)
                with open(fpath, "w") as fh:
                    fh.write(fcontent)
                # Parse octal mode string to int and apply.
                try:
                    mode_bits = int(fmode, 8)
                    os.chmod(fpath, mode_bits)
                except (ValueError, TypeError):
                    os.chmod(fpath, 0o600)
            except Exception as exc:
                print(
                    f"[runner] warning: failed to write credential file "
                    f"{f.get('path', '?')} for task {task_id}: {exc}",
                    flush=True,
                )


    # Build the FAIL-CLOSED sandbox plan. The env is assembled from an explicit
    # allowlist (never os.environ.copy) and NEVER carries BLUEPRINT_RUNNER_TOKEN /
    # BLUEPRINT_ORCHESTRATOR_URL into the task.  The backend-assigned pooled LLM
    # credentials flow through injected_env so they reach the agent without the
    # runner needing its own static LLM credentials.
    injected_env = {
        "AGENT_SECRET_TOKEN": agent_token,
        "ORCHESTRATOR_URL": orchestrator_url,
        "TASK_ID": task_id,
        "VM_INSTANCE_ID": vm_instance_id,
    }
    if llm_env:
        injected_env.update(llm_env)
    plan = iso.plan_agent_execution(
        caps=caps,
        agent_binary=agent_binary,
        workspace_host=workspace,
        injected_env=injected_env,
        source_env=dict(os.environ),
        task_type=task_type,
        agent_image=plan_image,
    )
    if plan.declined:
        print(
            f"[runner] DECLINED agent task {task_id} (fail-closed): {plan.decline_reason}",
            flush=True,
        )
        return False

    # Open a per-task log file to persist the agent's stdout/stderr output
    # on disk (best-effort - a write failure must never prevent the task from
    # running).  The reader thread below tees output to both this log file and
    # the runner's stdout (terminal/journal).
    _log_file_handle: "typing.BinaryIO | None" = None
    try:
        _log_path = _task_log_path(task_id)
        os.makedirs(os.path.dirname(_log_path), exist_ok=True)
        _log_file_handle = open(_log_path, "ab")
    except Exception as exc:
        print(
            f"[runner] warning: could not open log file for task {task_id}: {exc}",
            flush=True,
        )

    popen_kwargs: dict = {"env": plan.env, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(plan.argv, **popen_kwargs)
    except Exception as exc:
        if _log_file_handle is not None:
            try:
                _log_file_handle.close()
            except Exception:
                pass
        print(f"[runner] failed to spawn agent for task {task_id}: {exc}", flush=True)
        return False

    # Reader thread: tee the agent's stdout to both the per-task log file and
    # the runner's stdout (so output still appears in the terminal / systemd
    # journal, just as it did before with stdout=sys.stdout).
    def _tee_agent_output() -> None:
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                if _log_file_handle is not None:
                    _log_file_handle.write(chunk)
                    _log_file_handle.flush()
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
        except (ValueError, OSError):
            return  # stdout closed underneath us (normal on teardown)
        finally:
            if _log_file_handle is not None:
                try:
                    _log_file_handle.close()
                except Exception:
                    pass

    _log_reader = threading.Thread(target=_tee_agent_output, daemon=True)
    _log_reader.start()

    with lock:
        active_tasks[task_id] = proc
        agent_started_at[task_id] = time.monotonic()
        # Parallel map keyed by task_id: the reaper/shutdown teardown needs the
        # backend + container_name to issue `docker stop` for the docker backend
        # (bwrap → container_name None → group-signal only). Mirrors compute.
        agent_teardown[task_id] = (plan.backend, plan.container_name)
    print(
        f"[START] task_id={task_id} type={task_type} vm={vm_instance_id} "
        f"workspace={workspace} pid={proc.pid} sandbox={plan.backend}"
        + (f" container={plan.container_name}" if plan.container_name else ""),
        flush=True,
    )
    return True


def main() -> None:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        # Print the module docstring (which carries the RUNNER_RUNBOOK) and exit.
        print(__doc__ or "", flush=True)
        return

    check_mode = "--check" in sys.argv[1:]

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    token = os.environ.get("BLUEPRINT_RUNNER_TOKEN", "").strip()
    if token:
        # Persist for future restarts so re-enrolling after a reboot is unnecessary.
        _save_runner_token(token)
    else:
        token = _load_runner_token() or ""
        if not token:
            cred_path = _credential_path()
            print(
                "ERROR: BLUEPRINT_RUNNER_TOKEN is not set and no saved credential was found.",
                file=sys.stderr,
            )
            print(
                "  Set it:   export BLUEPRINT_RUNNER_TOKEN=<token>",
                file=sys.stderr,
            )
            print(
                "  Re-enroll: create a runner in the admin UI (Settings → Runners) to get a new token.",
                file=sys.stderr,
            )
            print(
                f"  (credential file checked: {cred_path})",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"[runner] token loaded from credential file ({_credential_path()})",
            flush=True,
        )
    orchestrator_url = _require_env("BLUEPRINT_ORCHESTRATOR_URL").rstrip("/")
    agent_binary = _locate_agent_binary()

    # Guarded env parsing: a malformed knob must NOT crash the runner into a
    # systemd restart loop — fall back to the default with a clear warning, and
    # clamp concurrency floors to 1 so a 0/-1 can never wedge the runner idle.
    max_concurrent = _env_int("BLUEPRINT_RUNNER_MAX_CONCURRENT", 2, minimum=1)
    max_concurrent_compute = _env_int(
        "BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS", 2, minimum=1
    )
    base_poll_interval = _env_float("BLUEPRINT_RUNNER_POLL_INTERVAL", 2.0, minimum=0.5)
    agent_max_seconds = _env_float(
        "BLUEPRINT_RUNNER_AGENT_MAX_SECONDS", 14400.0, minimum=60.0
    )
    max_auth_failures = _env_int("BLUEPRINT_RUNNER_MAX_AUTH_FAILURES", 20, minimum=0)
    max_network_failures = _env_int("BLUEPRINT_RUNNER_MAX_NETWORK_FAILURES", 20, minimum=0)
    failure_pause_threshold = _env_int("BLUEPRINT_RUNNER_FAILURE_PAUSE_THRESHOLD", 0, minimum=0)
    max_cpu_load = _env_float("BLUEPRINT_RUNNER_MAX_CPU_LOAD", 0.0, minimum=0.0)
    max_gpu_util_pct = _env_int("BLUEPRINT_RUNNER_MAX_GPU_UTIL_PCT", 0, minimum=0)

    # Prod agent container image (agent/Dockerfile). The default agent backend is bwrap
    # (host toolchain); the docker backend is used only on an explicit opt-in:
    #   BLUEPRINT_AGENT_BACKEND=docker   explicit docker selection
    #   BLUEPRINT_AGENT_BACKEND=bwrap    force bwrap (override even if image is set)
    #   BLUEPRINT_AGENT_BACKEND=auto     bwrap-first; docker only if image is set by
    #                                    the operator (e.g. direct BLUEPRINT_AGENT_IMAGE
    #                                    env, not auto-injected by blueprint-runner)
    # blueprint-runner no longer auto-injects BLUEPRINT_AGENT_IMAGE — the launcher
    # requires --docker-agent or BLUEPRINT_AGENT_BACKEND=docker to enable the docker
    # path, so this env var is only present when the operator explicitly chose docker.
    agent_image = os.environ.get("BLUEPRINT_AGENT_IMAGE", "").strip() or None
    agent_backend_pref = (
        os.environ.get("BLUEPRINT_AGENT_BACKEND", "").strip().lower() or "auto"
    )
    if agent_backend_pref == "bwrap":
        if agent_image is not None:
            print(
                "[runner] BLUEPRINT_AGENT_BACKEND=bwrap — ignoring BLUEPRINT_AGENT_IMAGE "
                "and using the host bwrap agent backend.",
                flush=True,
            )
        agent_image = None
    elif agent_backend_pref == "docker":
        print(
            "[runner] BLUEPRINT_AGENT_BACKEND=docker — docker agent path enabled.",
            flush=True,
        )
        # agent_image kept as-is; the fail-closed image-present check below applies.

    caps = _get_capabilities()

    # FAIL-CLOSED image check (Fix 3): a docker-usable box with BLUEPRINT_AGENT_IMAGE
    # set but the image ABSENT would still advertise agent_available=True and drain
    # tasks whose `docker run` then dies with nothing reported → strand/loop. Verify
    # the image exists ONCE here at startup (not per-heartbeat); if it does not,
    # disable the docker-agent path (agent_image=None) so the runner falls back to
    # the host bwrap backend — or declines agent work — instead of claim-and-crash.
    if agent_image is not None and caps.docker_usable and not _agent_image_present(
        agent_image
    ):
        print(
            f"[runner] ERROR: BLUEPRINT_AGENT_IMAGE={agent_image!r} is not present "
            "locally (`docker image inspect` failed) — DISABLING the docker agent "
            "backend. Pull/build the image (or fix the tag) and restart. Falling "
            "back to the host bwrap agent backend, or declining agent work if bwrap "
            "is unavailable.",
            flush=True,
        )
        agent_image = None

    # LOUD warning (Fix 2): a docker-only box (docker + agent image usable, but NO
    # bwrap) can run ONLY the read-only agent slice (iso.AGENT_DOCKER_TYPES). The
    # runner CANNOT self-restrict its task_types — that is operator-configured
    # server-side and the heartbeat handler preserves it — so the orchestrator can
    # still pin a write/heavy type here, which then declines-after-dispatch and
    # strands/loops the task. Until the end-to-end fix lands (orchestrator honoring
    # a runner-advertised task_types), the operator MUST restrict this runner's
    # enrolled task_types to the read-only slice. No warning when bwrap is also
    # present — bwrap can attempt the rest.
    if agent_image is not None and caps.docker_usable and not caps.bwrap_ok:
        _slice = ", ".join(sorted(iso.AGENT_DOCKER_TYPES))
        print(
            "[runner] ============================================================\n"
            "[runner] WARNING: docker-ONLY agent backend (docker + agent image "
            "present, but bubblewrap is NOT available).\n"
            "[runner] This runner can run ONLY the READ-ONLY agent slice in the prod "
            "image; it CANNOT run write/heavy agent types (create/fix/refactor/...).\n"
            "[runner] The runner CANNOT self-restrict its task_types (operator-set "
            "server-side; the heartbeat preserves it), so you MUST restrict this "
            "runner's enrolled task_types to exactly the read-only slice below, or "
            "the orchestrator may pin an unsupported type here that gets DECLINED "
            "after dispatch — stranding/looping the task.\n"
            f"[runner] read-only slice (iso.AGENT_DOCKER_TYPES): {_slice}\n"
            "[runner] (Install bubblewrap to let this runner attempt the rest via "
            "the host bwrap backend.)\n"
            "[runner] ============================================================",
            flush=True,
        )

    gpu_state = _make_gpu_state(caps)

    # Best-effort startup sweep of any stray tagged containers from a prior crash.
    iso.sweep_stale_containers(caps)

    result = _preflight(
        orchestrator_url, token, agent_binary, caps, gpu_state,
        max_concurrent, max_concurrent_compute, agent_image,
    )
    claimable = result["claimable"]
    gpu_capabilities = result["gpu_caps"]
    agent_claimable = claimable[iso.CLASS_AGENT]
    compute_claimable = claimable[iso.CLASS_COMPUTE]

    if check_mode:
        # Claim no work; exit reflects whether the box is ready.
        sys.exit(0 if result["setup_ok"] else 1)

    # Hard prereq: orchestrator must be reachable. A 401 is NOT fatal (the row may
    # be inactive and the heartbeat re-arms it) — mirrors the prior behavior.
    if not result["orchestrator_ok"]:
        print(
            f"ERROR: orchestrator unreachable at boot: {result['ping_error']}",
            file=sys.stderr,
        )
        sys.exit(1)
    if result["token_401"]:
        print(
            "[runner] WARNING: initial heartbeat returned 401 — token may be stale or "
            "the runner row inactive; entering poll loop and retrying (heartbeat re-arms it).",
            flush=True,
        )
    elif not result["token_valid"]:
        print("ERROR: initial heartbeat rejected the runner token.", file=sys.stderr)
        sys.exit(1)

    enrolled_max_concurrent = result.get("enrolled_max_concurrent")
    if enrolled_max_concurrent is not None and max_concurrent != enrolled_max_concurrent:
        print(
            f"[runner] WARNING: BLUEPRINT_RUNNER_MAX_CONCURRENT={max_concurrent} disagrees "
            f"with the server-enrolled preset max_concurrent={enrolled_max_concurrent}. "
            f"The orchestrator uses the server-side limit; set "
            f"BLUEPRINT_RUNNER_MAX_CONCURRENT={enrolled_max_concurrent} to match the "
            f"enrollment and avoid claiming more (or fewer) tasks than declared.",
            flush=True,
        )

    if not (agent_claimable or compute_claimable):
        print(
            "[runner] No task class is claimable on this host (no isolation boundary). "
            "Staying online (heartbeat only) — install bubblewrap/apptainer and restart "
            "to enable work.",
            flush=True,
        )

    # Shared state
    active_tasks: dict[str, subprocess.Popen] = {}
    # Per-agent monotonic start time, for the reaper's wall-clock deadline (I4).
    agent_started_at: dict[str, float] = {}
    # Per-agent (backend, container_name) so the reaper/shutdown teardown can issue
    # `docker stop` for the docker backend instead of leaking the container (bwrap
    # tasks map to (BACKEND_BWRAP, None) → group-signal only). Keyed by task_id.
    agent_teardown: dict[str, tuple[str, str | None]] = {}
    lock = threading.Lock()
    # SEPARATE capacity pools (I5): agents and compute never contend for one
    # shared semaphore, so the advertised concurrency is honest and a saturated
    # agent pool can't starve compute (or vice-versa).
    agent_capacity = threading.BoundedSemaphore(max_concurrent)
    compute_capacity = threading.BoundedSemaphore(max_concurrent_compute)
    telemetry = _TelemetryState()
    workspace_root = _workspace_root()

    active_compute_jobs: set[str] = set()
    compute_lock = threading.Lock()

    reaper = threading.Thread(
        target=_reap_loop,
        args=(active_tasks, lock, agent_capacity),
        kwargs={
            "agent_started_at": agent_started_at,
            "agent_max_seconds": agent_max_seconds,
            "telemetry": telemetry,
            "agent_teardown": agent_teardown,
        },
        daemon=True,
    )
    reaper.start()

    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(
            orchestrator_url,
            token,
            active_tasks,
            lock,
            gpu_capabilities,
            active_compute_jobs,
            compute_lock,
            caps.bwrap_ok,  # agent_sandbox_ok — gates the bwrap advertisement leg
        ),
        kwargs={
            "telemetry": telemetry,
            "workspace_root": workspace_root,
            "max_auth_failures": max_auth_failures,
            "max_network_failures": max_network_failures,
            # Docker advertisement leg: advertise (read-only) agent work on a
            # docker+image box even without bwrap.
            "caps": caps,
            "agent_image": agent_image,
        },
        daemon=True,
    )
    heartbeat.start()

    # Threads the supervisor treats as load-bearing: if any dies while we are NOT
    # shutting down, the process exits non-zero so systemd restarts it (I3).
    background_threads: dict[str, threading.Thread] = {
        "reaper": reaper,
        "heartbeat": heartbeat,
    }

    # Only poll for compute jobs when a compute boundary can be built. Per-job
    # DECLINE (e.g. imageless on a container-only box) is still enforced in
    # _run_compute_job; this avoids pulling work we plainly cannot sandbox.
    if compute_claimable:
        compute_poll = threading.Thread(
            target=_compute_poll_loop,
            args=(
                orchestrator_url,
                token,
                active_compute_jobs,
                compute_lock,
                max_concurrent_compute,
                compute_capacity,
            ),
            kwargs={"base_poll_interval": base_poll_interval, "capabilities": caps,
                    "gpu_state": gpu_state, "telemetry": telemetry,
                    "max_gpu_util_pct": max_gpu_util_pct},
            daemon=True,
        )
        compute_poll.start()
        background_threads["compute_poll"] = compute_poll

    poll_interval = base_poll_interval
    next_task_url = f"{orchestrator_url}/api/runner/next-task"
    min_free_disk = _min_free_disk_bytes()

    # An agent sandbox exists iff bwrap can be built OR a docker+image backend is
    # available. When agent_image is None this is exactly caps.bwrap_ok (unchanged).
    agent_sandbox_ok = caps.bwrap_ok or _docker_agent_possible(caps, agent_image)

    # Agent poll loop — only runs when an agent sandbox can be built (fail-closed).
    # Otherwise we idle on the shutdown event while compute/heartbeat continue.
    # The whole body is wrapped so a transient error keeps the runner alive; the
    # supervisor check makes an actually-dead background thread fatal.
    while not _shutdown_event.is_set():
        try:
            _supervise_threads(background_threads)

            if not agent_sandbox_ok:
                # No agent sandbox (no bwrap, no docker+image) — never claim agent
                # work. Wait for shutdown.
                time.sleep(1)
                continue

            # During an auto-update, don't claim new work — the update may restart
            # the process on success. Unlike _shutdown_event, this event is scoped
            # only to work claiming: reaper, compute poller, and supervise continue
            # running, so a transient update failure never kills healthy threads.
            if _update_in_progress.is_set():
                time.sleep(poll_interval)
                continue

            with lock:
                active_count = len(active_tasks)

            if active_count >= max_concurrent:
                time.sleep(poll_interval)
                continue

            if not _try_acquire_capacity(agent_capacity):
                time.sleep(poll_interval)
                continue

            # Low-disk guard: don't claim new agent work when the workspace
            # filesystem is nearly full — starting a task that dies mid-run wastes
            # a claim and can wedge the disk further. Disabled by default (0).
            if _disk_below_min(workspace_root, min_free_disk):
                _release_capacity(agent_capacity)
                print(
                    f"[runner] low disk on {workspace_root} "
                    f"(< {min_free_disk} bytes free) — not claiming agent work this cycle.",
                    flush=True,
                )
                time.sleep(poll_interval)
                continue

            # Host-load guard: skip claiming when the machine is overloaded.
            if max_cpu_load > 0:
                load = _cpu_load_avg()
                if load is not None and load >= max_cpu_load:
                    _release_capacity(agent_capacity)
                    print(
                        f"[runner] cpu_load_avg {load} >= threshold {max_cpu_load}"
                        " — skipping agent claim",
                        flush=True,
                    )
                    time.sleep(poll_interval)
                    continue

            # Circuit-breaker: pause claiming after N consecutive task failures.
            if failure_pause_threshold > 0:
                pause_rem = telemetry.pause_remaining(failure_pause_threshold, _FAILURE_PAUSE_SECONDS)
                if pause_rem is not None and pause_rem > 0:
                    _release_capacity(agent_capacity)
                    # Log on the first iteration of each pause cycle (when
                    # pause_rem is close to the full pause_seconds duration).
                    if pause_rem >= _FAILURE_PAUSE_SECONDS - 1.0:
                        print(
                            f"[runner] CIRCUIT BREAKER: consecutive task failures "
                            f"(threshold={failure_pause_threshold}) — "
                            f"pausing task claiming for {_FAILURE_PAUSE_SECONDS:.0f}s. "
                            "A successful task will clear the circuit.",
                            flush=True,
                        )
                    time.sleep(min(pause_rem, poll_interval, 10.0))
                    continue

            try:
                resp = _post(next_task_url, token, {}, return_headers=True)
                if isinstance(resp, tuple) and len(resp) == 3:
                    status, body, retry_after = resp
                else:
                    status, body = resp
                    retry_after = None
            except Exception as exc:
                _release_capacity(agent_capacity)
                print(f"[runner] poll error: {exc}", flush=True)
                time.sleep(5)
                continue

            if status == 429:
                wait = retry_after if retry_after is not None else min(poll_interval * 2, 30.0)
                wait = max(0.0, min(wait, 300.0))
                print(f"[runner] poll 429 — backing off {wait:.1f}s (Retry-After)", flush=True)
                _release_capacity(agent_capacity)
                time.sleep(wait)
                continue

            if status == 401:
                print(
                    "[runner] poll 401 — runner token rejected; backing off and retrying "
                    "(row may be reactivating via heartbeat).",
                    flush=True,
                )
                _release_capacity(agent_capacity)
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status == 503 and isinstance(body, dict) and body.get("pool_exhausted"):
                _release_capacity(agent_capacity)
                print(
                    "[runner] POOL EXHAUSTED — no LLM account available; "
                    "task stays claimable for a future poll cycle",
                    flush=True,
                )
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status == 204 or body is None:
                _release_capacity(agent_capacity)
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status != 200:
                _release_capacity(agent_capacity)
                print(f"[runner] poll warning: HTTP {status}", flush=True)
                time.sleep(5)
                continue

            poll_interval = base_poll_interval

            task_id = body.get("task_id") or body.get("id", "unknown")
            agent_token = body.get("agent_token", "")
            task_type = body.get("task_type", "unknown")
            vm_instance_id = body.get("vm_instance_id") or "runner:local"
            # Per-task server-vended LLM credentials from the nested
            # {"credentials": {"env": {...}, "files": [...]}} response shape.
            # The env dict (PROVIDER, AUTH_MODE, API keys) is used both as
            # llm_env (injected into the agent subprocess) and as credential_env
            # (passed to the isolation layer's per_task_creds).  The files list
            # is used as credential_files (written to the workspace bind-mount
            # before spawn) and as llm_files (written inside the sandbox).
            _creds = body.get("credentials") or {}
            credential_env: dict[str, str] = _creds.get("env", {})
            credential_files: list[dict[str, typing.Any]] = _creds.get("files", [])
            llm_env = credential_env
            llm_files = credential_files
            if credential_env:
                # Provider and model are read from the credential_env dict,
                # not from top-level response fields (the runner response nests
                # them inside credentials.env). Account ID/name are not currently
                # returned by the next-task endpoint.
                provider = credential_env.get("PROVIDER", "")
                model = credential_env.get("LLM_MODEL", "")
                print(
                    f"[runner] task {task_id}: server-vended credentials "
                    f"provider={provider} model={model} "
                    f"env_keys={sorted(credential_env.keys())}",
                    flush=True,
                )
            elif not any(
                os.environ.get(k, "").strip() for k in _PROVIDER_CREDENTIAL_KEYS
            ):
                print(
                    f"[runner] task {task_id}: NO credentials — server returned no "
                    f"credential_env and no static host provider credentials are set. "
                    f"The agent WILL fail at LLM call.",
                    flush=True,
                )

            with lock:
                already_active = task_id in active_tasks
            if already_active:
                print(f"[runner] skipping duplicate dispatch for task {task_id}", flush=True)
                _release_capacity(agent_capacity)
                time.sleep(poll_interval)
                continue

            # Refresh the host agent dist lazily (only the bwrap backend needs it;
            # the docker backend carries the agent in the image). _spawn_agent_task
            # decides which backend applies and declines fail-closed if neither can
            # run THIS task type.
            if not agent_binary:
                agent_binary = _locate_agent_binary()

            try:
                workspace = _agent_workspace_for_task(task_id)
            except Exception as exc:
                print(f"[runner] failed to prepare workspace for task {task_id}: {exc}", flush=True)
                _release_capacity(agent_capacity)
                time.sleep(poll_interval)
                continue

            spawned = _spawn_agent_task(
                task_id=task_id,
                task_type=task_type,
                agent_token=agent_token,
                vm_instance_id=vm_instance_id,
                orchestrator_url=orchestrator_url,
                workspace=workspace,
                caps=caps,
                agent_binary=agent_binary,
                agent_image=agent_image,
                active_tasks=active_tasks,
                agent_started_at=agent_started_at,
                agent_teardown=agent_teardown,
                lock=lock,
                llm_env=llm_env,
                llm_files=llm_files,
            )
            if not spawned:
                # DECLINE / spawn failure — the reaper never sees this task, so the
                # permit must be released here.
                _release_capacity(agent_capacity)
                # Notify the orchestrator so the task returns to QUEUED instead of
                # stranding in PROVISIONING until the runner goes offline.
                _notify_decline(orchestrator_url, token, task_id)
                time.sleep(poll_interval)
                continue
        except Exception as exc:  # noqa: BLE001 — a transient error must not crash the runner
            print(f"[runner] agent poll loop error (continuing): {exc}", flush=True)
            time.sleep(5)

    # Drain active compute jobs first — daemon threads would be killed on
    # process exit, so give them a chance to report their final status.
    _wait_for_compute_jobs(active_compute_jobs, compute_lock, orchestrator_url, token)

    # Graceful shutdown (monotonic clock so a wall-clock jump can't skew the wait).
    deadline = time.monotonic() + 120

    with lock:
        remaining = dict(active_tasks)

    if remaining:
        print(f"[runner] Shutdown requested — waiting for {len(remaining)} active agents...", flush=True)
        for tid, proc in remaining.items():
            timeout = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                backend, container_name = agent_teardown.get(tid, (None, None))
                print(f"[runner] timeout waiting for task {tid} (pid={proc.pid}) — tearing down group", flush=True)
                # docker backend → also `docker stop <name>` so the container is not
                # left running past shutdown (bwrap → container_name None → group only).
                iso.terminate_process_tree(
                    proc, backend=backend, container_name=container_name
                )

    # Drain compute job threads. These are daemon threads: when the process exits
    # they are killed mid-flight, before _run_compute_job can send a terminal
    # status report to the server. Wait for them to finish (they check
    # _shutdown_event and self-terminate with a "failed" report); if any are still
    # alive after the deadline, report failure on their behalf.
    with compute_lock:
        remaining_compute = set(active_compute_jobs)
    if remaining_compute:
        print(
            f"[runner] Shutdown requested — waiting for {len(remaining_compute)} active compute jobs...",
            flush=True,
        )
        while time.monotonic() < deadline:
            time.sleep(1)
            with compute_lock:
                if not active_compute_jobs:
                    break
        # After the deadline: any compute job still alive was stranded by a daemon
        # thread that couldn't finish. Report it as failed so it doesn't remain
        # stuck in RUNNING on the server.
        with compute_lock:
            stranded = set(active_compute_jobs)
        if stranded:
            for job_id in stranded:
                print(
                    f"[runner] compute job {job_id} did not finish within shutdown "
                    "deadline — reporting as failed",
                    flush=True,
                )
                try:
                    _report_compute_job(
                        orchestrator_url, token, job_id,
                        exec_status="failed", exit_code=1,
                        command_log={
                            "text": "Runner shut down (SIGTERM) before job completed",
                            "truncated": False,
                        },
                    )
                except Exception:
                    pass
            with compute_lock:
                active_compute_jobs.difference_update(stranded)

    print("[runner] Exiting.", flush=True)


if __name__ == "__main__":
    main()
