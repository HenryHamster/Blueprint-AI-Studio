"""Blueprint local fleet runner.

Pull-based runner process for operator hardware. Polls the cloud orchestrator
for pinned tasks and spawns the Blueprint agent binary for each claimed task.

Usage:
    python3 scripts/local_runner.py

Required environment variables:
    BLUEPRINT_RUNNER_TOKEN      One-time enroll token from the admin UI: Runtime tab →
                                      Devices and runners → click "Enroll runner" (default)
                                      or "Strategic runner" (strategic_local preset).
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
    BLUEPRINT_RUNNER_RESOURCE_ADMISSION Enable host-wide CPU/RAM/disk admission across
                                      both agent and compute pools (default: 1). Set 0 only
                                      for compatibility troubleshooting.
    BLUEPRINT_RUNNER_AGENT_CPU_RESERVATION_CORES / _MEMORY_RESERVATION_BYTES
                                      Per-agent reservation before claiming work (defaults:
                                      1 core, 2 GiB).
    BLUEPRINT_RUNNER_COMPUTE_CPU_RESERVATION_CORES / _MEMORY_RESERVATION_BYTES
                                      Per-compute-job reservation (defaults: 2 cores, 4 GiB).
    BLUEPRINT_RUNNER_RESERVED_CPU_CORES / _RESERVED_MEMORY_BYTES
                                      Capacity held back for the host/control plane (defaults:
                                      1 core, 1 GiB; CPU default is 0 on a one-vCPU host).
    BLUEPRINT_RUNNER_MIN_FREE_MEMORY_BYTES
                                      Required observed available RAM after a reservation
                                      (default: 1 GiB).
    BLUEPRINT_RUNNER_AGENT_DISK_RESERVATION_BYTES / _COMPUTE_DISK_RESERVATION_BYTES
                                      Per-workload workspace-disk reservation (default: 0).
    BLUEPRINT_RUNNER_GPU_INDICES      Comma-separated GPU-index allowlist (default: all)
    BLUEPRINT_RUNNER_GPUS_PER_JOB     GPUs assigned per compute job (default: 1)
    BLUEPRINT_RUNNER_GPU_PROBE_IMAGE  Image used to prove container GPU passthrough.
                                      Set this to a pullable project/private image with
                                      nvidia-smi when Docker Hub is unavailable; default
                                      is nvidia/cuda:12.6.3-base-ubuntu24.04.
    BLUEPRINT_RUNNER_CPU_LIMIT        Best-effort CPU cap (cores) per task
    BLUEPRINT_RUNNER_MEM_LIMIT        Best-effort memory cap per task (e.g. 8g)
    BLUEPRINT_RUNNER_CLI_MAINTENANCE  Host CLI self-maintenance (default: 1;
                                      set 0/false/no/off to disable).
    BLUEPRINT_RUNNER_CLI_DIR          Blueprint-managed CLI release root
                                      (default: ~/.blueprint/managed-clis).
    BLUEPRINT_RUNNER_CLI_MAINTENANCE_INTERVAL
                                      Recheck interval seconds (default: 21600,
                                      floor 60). Failed installs also use
                                      persisted exponential backoff.

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

import base64
import http.client
import io
import json
import math
import typing
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import random
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


# PID-1 programs that actually SUPERVISE (restart) their managed children.
# `tini` is deliberately absent: it forwards signals and reaps zombies but never
# restarts anything, so treating it as a supervisor would be the same lie this
# detector exists to stop telling.
_SUPERVISING_INITS = ("supervisord", "runit", "runsv", "s6-svscan")


def _pid1_comm() -> str:
    """``/proc/1/comm`` (the PID-1 program name), or "" when unreadable."""
    try:
        with open("/proc/1/comm", "r") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def _self_cgroup_text() -> str:
    """``/proc/self/cgroup`` contents, or "" when unreadable."""
    try:
        with open("/proc/self/cgroup", "r") as fh:
            return fh.read()
    except Exception:
        return ""


def _cgroup_path(cgroup_text: str) -> str:
    """First cgroup path in *cgroup_text* (``0::/user.slice/...`` → the path)."""
    for line in cgroup_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        return parts[2] if len(parts) == 3 else line
    return ""


def _systemd_service_unit(cgroup_text: str) -> str | None:
    """Return the systemd ``*.service`` unit owning this process, else None.

    This is THE discriminator between "systemd exists on this host" and "systemd
    is actually supervising this process". A runner launched from a login shell
    on a systemd host lives in ``/user.slice/user-1000.slice/session-3.scope`` —
    a *scope*, which systemd tracks but NEVER restarts. Only a ``.service`` unit
    carries a ``Restart=`` policy, so only a ``.service`` cgroup means "systemd
    will bring this back".
    """
    for line in cgroup_text.splitlines():
        parts = line.split(":", 2)
        path = parts[2] if len(parts) == 3 else line
        for component in reversed(path.strip().split("/")):
            if component.endswith(".service"):
                return component
    return None


def _parent_pid(pid: int) -> int | None:
    """PPid of *pid* from ``/proc/<pid>/status``, or None when unreadable."""
    try:
        with open(f"/proc/{pid}/status", "r") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _ancestry_reaches_pid1(max_hops: int = 64) -> bool:
    """True iff this process's parent chain actually reaches PID 1.

    A ``docker exec``-launched process inside a supervised container is NOT in
    the init's process tree (its parent chain dead-ends at PPid 0), so the init
    would never restart it. Walking the chain is what makes the PID-1 signal an
    honest claim about *this* process rather than about the host.
    """
    pid = os.getpid()
    for _ in range(max_hops):
        if pid == 1:
            return True
        ppid = _parent_pid(pid)
        if ppid is None or ppid <= 0:
            return False
        pid = ppid
    return False


def _detect_supervisor_state() -> dict:
    """Report whether a supervisor will ACTUALLY restart this runner.

    Returns ``{"supervisor": str | None, "supervised": bool, "evidence": str,
    "detail": str}``. ``supervised`` is True only when a positive, process-scoped
    signal says something will respawn this process after it exits — never merely
    because a supervisor exists somewhere on the host.

    Positive signals (any hit wins):
      * ``INVOCATION_ID`` / ``JOURNAL_STREAM`` env → systemd launched THIS process.
      * ``SUPERVISOR_ENABLED`` env → supervisord launched THIS process.
      * ``BLUEPRINT_RUNNER_SUPERVISED=1`` → operator-declared restart wrapper
        (documented in the RUNNER_RUNBOOK so a ``while true`` loop can opt in).
      * PID 1 is systemd AND this process's cgroup is a systemd ``*.service``
        unit AND its parent chain reaches PID 1.
      * PID 1 is a supervising init (supervisord/runit/s6) AND this process's
        parent chain reaches PID 1.

    Explicitly NEGATIVE (the bug this replaces): PID 1 being ``systemd`` on its
    own. Every process on a systemd host has systemd as PID 1, including a runner
    started by hand in tmux. The old detector returned "systemd" for all of them,
    so the self-update survival path took the "supervisor will restart me" branch,
    ``os._exit(0)``-ed, and nothing ever came back — while ``--check`` printed a
    healthy "systemd (auto-restart on crash/update)". Both now tell the truth.
    """
    if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
        return {
            "supervisor": "systemd",
            "supervised": True,
            "evidence": "systemd unit env (INVOCATION_ID/JOURNAL_STREAM)",
            "detail": "launched by a systemd unit — it restarts the runner if Restart= is set",
        }
    if os.environ.get("SUPERVISOR_ENABLED"):
        return {
            "supervisor": "supervisord",
            "supervised": True,
            "evidence": "supervisord env (SUPERVISOR_ENABLED)",
            "detail": "launched by supervisord — it restarts the runner on exit",
        }
    if os.environ.get("BLUEPRINT_RUNNER_SUPERVISED") == "1":
        return {
            "supervisor": "declared",
            "supervised": True,
            "evidence": "operator-declared (BLUEPRINT_RUNNER_SUPERVISED=1)",
            "detail": "operator declared an external restart wrapper (e.g. a `while true` loop)",
        }

    pid1 = _pid1_comm()
    if pid1 == "systemd":
        unit = _systemd_service_unit(_self_cgroup_text())
        if unit and _ancestry_reaches_pid1():
            return {
                "supervisor": "systemd",
                "supervised": True,
                "evidence": f"systemd service cgroup ({unit})",
                "detail": f"running inside systemd unit {unit} — it restarts the runner if Restart= is set",
            }
        cgroup = _cgroup_path(_self_cgroup_text()) or "unknown"
        return {
            "supervisor": None,
            "supervised": False,
            "evidence": f"pid1=systemd but cgroup {cgroup} is not a systemd .service unit",
            "detail": (
                "systemd is PID 1 on this HOST, but this runner is NOT running as a "
                "systemd service unit (looks like a manual tmux/nohup/screen launch). "
                "systemd will NOT restart it — a crash is permanent"
            ),
        }
    if pid1 in _SUPERVISING_INITS:
        if _ancestry_reaches_pid1():
            return {
                "supervisor": pid1,
                "supervised": True,
                "evidence": f"pid1={pid1} and this process is in its process tree",
                "detail": f"container init {pid1} supervises this process and restarts it on exit",
            }
        return {
            "supervisor": None,
            "supervised": False,
            "evidence": f"pid1={pid1} but this process is NOT in its process tree (docker exec?)",
            "detail": (
                f"{pid1} is PID 1 but this process was not started by it (parent chain "
                "does not reach PID 1) — it will NOT be restarted"
            ),
        }
    if pid1 == "tini":
        return {
            "supervisor": None,
            "supervised": False,
            "evidence": "pid1=tini (init/zombie-reaper — does not restart children)",
            "detail": (
                "tini forwards signals and reaps zombies but never restarts the runner. "
                "Rely on the container restart policy and set BLUEPRINT_RUNNER_SUPERVISED=1 "
                "to declare it"
            ),
        }
    return {
        "supervisor": None,
        "supervised": False,
        "evidence": f"no supervisor signal (pid1={pid1 or 'unknown'})",
        "detail": (
            "no process supervisor detected — a crash will NOT restart the runner"
        ),
    }


def _detect_process_supervisor() -> str | None:
    """Name of the supervisor that will ACTUALLY restart this runner, else None.

    Thin wrapper over :func:`_detect_supervisor_state` — None means the runner is
    genuinely unsupervised (manual tmux/nohup/screen launch, or a systemd host
    that is not running the runner as a service unit), which is what makes the
    self-update path re-exec in place instead of exiting into the void.
    """
    state = _detect_supervisor_state()
    return state["supervisor"] if state.get("supervised") else None


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


# ── Blueprint-managed provider CLIs ─────────────────────────────────────────

_MANAGED_CLI_POLICY: dict[str, tuple[str, str]] = {
    # Codex accepts compatible newer versions, but repair/bootstrap uses the
    # reviewed exact version so a registry change cannot silently alter runners.
    "codex": ("@openai/codex", "0.144.1"),
    # Preserve the existing Docker/Packer policy for Claude Code: current latest.
    "claude": ("@anthropic-ai/claude-code", "latest"),
}
_CLI_MAINTENANCE_LOCK = threading.Lock()
_CLI_MAINTENANCE_INTERVAL_SECONDS = 6 * 60 * 60
_CLI_INSTALL_TIMEOUT_SECONDS = 180
_CLI_LOCK_STALE_SECONDS = 15 * 60
_CLI_LOCK_PUBLICATION_GRACE_SECONDS = 30
_CLI_LOCK_OWNER_FILE = "owner.json"
_CLI_SUBPROCESS_ENV_KEYS = (
    # Executable discovery and user-level npm configuration.
    "PATH",
    "HOME",
    "USERPROFILE",
    # Locale and terminal basics.
    "LANG",
    "LC_ALL",
    "TERM",
    # Temporary directories.
    "TMPDIR",
    "TMP",
    "TEMP",
    # Registry/network transport without provider or runner credentials.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    # Windows process-launch essentials.
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "ComSpec",
    "COMSPEC",
    "PATHEXT",
)


def _cli_subprocess_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return the minimal environment trusted CLI install/probe subprocesses need.

    npm lifecycle code and newly downloaded CLI shims are executable code. They
    must not inherit enrollment, orchestrator, GitHub, or provider credentials
    from the long-lived runner process.
    """
    values = os.environ if source is None else source
    return {
        key: value
        for key in _CLI_SUBPROCESS_ENV_KEYS
        if isinstance((value := values.get(key)), str) and value
    }


def _managed_cli_base() -> str:
    override = os.environ.get("BLUEPRINT_RUNNER_CLI_DIR", "").strip()
    return os.path.abspath(
        os.path.expanduser(
            override or os.path.join(_credential_dir(), "managed-clis")
        )
    )


def _atomic_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode())
    finally:
        os.close(fd)
    try:
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _managed_cli_bin(name: str) -> str | None:
    """Resolve a verified active release without trusting path traversal."""
    cli_root = os.path.join(_managed_cli_base(), name)
    manifest = _read_json(os.path.join(cli_root, "active.json"))
    relative = manifest.get("release")
    if not isinstance(relative, str) or not relative:
        return None
    release = os.path.abspath(os.path.join(cli_root, relative))
    try:
        if os.path.commonpath((cli_root, release)) != os.path.abspath(cli_root):
            return None
    except ValueError:
        return None
    bin_dir = os.path.join(release, "node_modules", ".bin")
    return bin_dir if os.path.isdir(bin_dir) else None


def _activate_managed_cli_paths() -> list[str]:
    """Prepend verified Blueprint releases and remove superseded managed paths."""
    base = _managed_cli_base()
    current = os.environ.get("PATH", "").split(os.pathsep)
    retained: list[str] = []
    for entry in current:
        if not entry:
            continue
        try:
            inside = os.path.commonpath((base, os.path.abspath(entry))) == base
        except ValueError:
            inside = False
        if not inside:
            retained.append(entry)
    active = [
        path
        for name in _MANAGED_CLI_POLICY
        if (path := _managed_cli_bin(name)) is not None
    ]
    os.environ["PATH"] = os.pathsep.join([*active, *retained])
    return active


def _version_text(output: str) -> str | None:
    match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b", output)
    return match.group(1) if match else None


def _npm_latest_version(npm: str, package: str) -> str | None:
    try:
        result = subprocess.run(
            [npm, "view", package, "version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_cli_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _version_text(result.stdout or "") if result.returncode == 0 else None


def _cli_backoff_path(name: str) -> str:
    return os.path.join(_managed_cli_base(), name, "maintenance.json")


def _cli_in_backoff(name: str, now: float) -> bool:
    state = _read_json(_cli_backoff_path(name))
    retry_at = state.get("next_retry_at", 0)
    return isinstance(retry_at, (int, float)) and retry_at > now


def _record_cli_install_result(name: str, *, succeeded: bool, now: float) -> None:
    if succeeded:
        _atomic_json(
            _cli_backoff_path(name),
            {"failures": 0, "next_retry_at": 0, "last_success_at": now},
        )
        return
    old = _read_json(_cli_backoff_path(name))
    failures = min(int(old.get("failures", 0) or 0) + 1, 10)
    delay = min(6 * 60 * 60, 60 * (2 ** (failures - 1)))
    _atomic_json(
        _cli_backoff_path(name),
        {"failures": failures, "next_retry_at": now + delay},
    )


def _read_cli_lock_owner(path: str) -> dict | None:
    """Read valid owner metadata from an atomically held lock directory."""
    try:
        owner_path = os.path.join(path, _CLI_LOCK_OWNER_FILE)
        value = _read_json(owner_path)
        if not isinstance(value.get("owner_token"), str):
            return None
        if not isinstance(value.get("pid"), int):
            return None
        if not isinstance(value.get("created_at"), (int, float)):
            return None
        return value
    except (OSError, TypeError):
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    # On Windows, ``os.kill(pid, 0)`` is not the harmless existence probe it is
    # on POSIX. Signal 0 is ``CTRL_C_EVENT`` there, and GenerateConsoleCtrlEvent
    # can broadcast Ctrl+C to every process sharing the runner's console (for
    # example the operator's Codex CLI). Query the process handle instead.
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            # An access-denied process still exists; other failures mean there
            # is no safely queryable live process for this PID.
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _remove_cli_lock_directory(
    path: str,
    *,
    expected_owner_token: str | None,
    allow_unpublished: bool = False,
    before_rmdir=None,
) -> bool:
    """Remove owner metadata inside the held directory, then drop the directory.

    A successor cannot create the same directory until rmdir completes, so
    release has no pathname gap in which it can unlink a successor's lock.
    """
    owner = _read_cli_lock_owner(path)
    if expected_owner_token is not None:
        if owner is None or owner.get("owner_token") != expected_owner_token:
            return False
    elif not allow_unpublished:
        return False
    try:
        for name in os.listdir(path):
            if name == _CLI_LOCK_OWNER_FILE or (
                name.startswith(".owner.") and name.endswith(".tmp")
            ):
                os.remove(os.path.join(path, name))
        if before_rmdir is not None:
            before_rmdir()
        os.rmdir(path)
        return True
    except OSError:
        return False


def _reclaim_abandoned_cli_lock(path: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    if age < _CLI_LOCK_PUBLICATION_GRACE_SECONDS:
        return False
    owner = _read_cli_lock_owner(path)
    if owner is None:
        return _remove_cli_lock_directory(
            path, expected_owner_token=None, allow_unpublished=True
        )
    if _pid_is_alive(owner["pid"]):
        return False
    return _remove_cli_lock_directory(
        path, expected_owner_token=owner["owner_token"]
    )


def _acquire_cli_process_lock() -> tuple[str, str] | None:
    base = _managed_cli_base()
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, ".maintenance.lock")
    owner_token = secrets.token_hex(32)

    def _create() -> tuple[str, str]:
        os.mkdir(path, 0o700)
        owner_path = os.path.join(path, _CLI_LOCK_OWNER_FILE)
        temp_path = os.path.join(path, f".owner.{owner_token}.tmp")
        payload = json.dumps(
            {
                "owner_token": owner_token,
                "pid": os.getpid(),
                "created_at": time.time(),
            },
            sort_keys=True,
        ).encode()
        try:
            fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp_path, owner_path)
            return path, owner_token
        except OSError:
            _remove_cli_lock_directory(
                path, expected_owner_token=None, allow_unpublished=True
            )
            raise

    try:
        return _create()
    except FileExistsError:
        try:
            if _reclaim_abandoned_cli_lock(path):
                return _create()
        except (OSError, FileExistsError):
            pass
        return None
    except OSError:
        return None


def _release_cli_process_lock(
    held: tuple[str, str] | None,
) -> None:
    if held is None:
        return
    path, owner_token = held
    _remove_cli_lock_directory(path, expected_owner_token=owner_token)


def _prune_managed_cli_releases(
    releases: str, active_release: str, *, keep: int = 2
) -> None:
    """Keep the active release plus the newest rollback release, best-effort."""
    try:
        candidates = [
            os.path.join(releases, entry)
            for entry in os.listdir(releases)
            if entry.startswith("release-")
            and os.path.isdir(os.path.join(releases, entry))
        ]
        candidates.sort(key=os.path.getmtime, reverse=True)
        retained = {os.path.abspath(active_release)}
        retained.update(os.path.abspath(path) for path in candidates[:keep])
        for path in candidates:
            if os.path.abspath(path) not in retained:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _install_managed_cli(
    name: str,
    npm: str,
    package: str,
    target: str,
) -> bool:
    """Stage, verify, then atomically activate one Blueprint-owned CLI."""
    cli_root = os.path.join(_managed_cli_base(), name)
    releases = os.path.join(cli_root, "releases")
    os.makedirs(releases, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".stage-", dir=releases)
    subprocess_env = _cli_subprocess_env()
    try:
        result = subprocess.run(
            [
                npm,
                "install",
                "--prefix",
                staging,
                "--include=optional",
                "--omit=dev",
                "--no-audit",
                "--no-fund",
                "--loglevel=error",
                f"{package}@{target}",
            ],
            capture_output=True,
            text=True,
            timeout=_CLI_INSTALL_TIMEOUT_SECONDS,
            env=subprocess_env,
        )
        if result.returncode != 0:
            return False
        bin_dir = os.path.join(staging, "node_modules", ".bin")
        executable = shutil.which(name, path=bin_dir)
        if not executable:
            return False
        verified = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=subprocess_env,
        )
        info = iso._llm_cli_result(name, verified)
        if info.get("healthy") is not True:
            return False
        actual = _version_text(info.get("version") or "")
        if target != "latest" and actual != target:
            return False

        safe_version = re.sub(r"[^0-9A-Za-z._-]", "_", actual or target)
        unique = os.path.basename(staging).removeprefix(".stage-")
        final_name = f"release-{int(time.time())}-{safe_version}-{unique}"
        final_dir = os.path.join(releases, final_name)
        os.replace(staging, final_dir)
        staging = ""
        _atomic_json(
            os.path.join(cli_root, "active.json"),
            {
                "release": os.path.relpath(final_dir, cli_root),
                "version": actual or target,
            },
        )
        _activate_managed_cli_paths()
        _prune_managed_cli_releases(releases, final_dir)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


def _maintain_host_llm_clis_once(*, now: float | None = None) -> None:
    """Repair host CLIs without ever mutating operator-managed global installs."""
    if os.environ.get("BLUEPRINT_RUNNER_CLI_MAINTENANCE", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return
    npm = shutil.which("npm")
    if not npm:
        print("[runner] CLI maintenance skipped: npm is unavailable.", flush=True)
        return
    timestamp = time.time() if now is None else now
    with _CLI_MAINTENANCE_LOCK:
        held = _acquire_cli_process_lock()
        if held is None:
            return
        try:
            _activate_managed_cli_paths()
            inventory = iso.probe_llm_clis(env=_cli_subprocess_env())
            for name, (package, policy_target) in _MANAGED_CLI_POLICY.items():
                if _cli_in_backoff(name, timestamp):
                    continue
                info = inventory.get(name) or {}
                target: str | None = None
                if info.get("healthy") is not True:
                    target = policy_target
                elif name == "claude":
                    latest = _npm_latest_version(npm, package)
                    actual = _version_text(info.get("version") or "")
                    if latest and actual and latest != actual:
                        target = latest
                # Codex health already enforces >= the reviewed minimum. Healthy
                # compatible operator installs are left alone; repairs go managed.
                if target is None:
                    continue
                print(
                    f"[runner] maintaining Blueprint-managed {name} CLI "
                    f"(policy {target})...",
                    flush=True,
                )
                succeeded = _install_managed_cli(
                    name, npm, package, target
                )
                _record_cli_install_result(
                    name, succeeded=succeeded, now=timestamp
                )
                if succeeded:
                    print(
                        f"[runner] Blueprint-managed {name} CLI verified and activated.",
                        flush=True,
                    )
                else:
                    # Never echo npm stdout/stderr: registry configuration can
                    # contain credentials. Existing healthy capacity stays active.
                    print(
                        f"[runner] WARNING: managed {name} CLI install/verification "
                        "failed; keeping the previous CLI and backing off.",
                        flush=True,
                    )
        finally:
            _release_cli_process_lock(held)


def _host_cli_maintenance_loop() -> None:
    while not _shutdown_event.is_set():
        try:
            _maintain_host_llm_clis_once()
        except Exception as exc:
            print(
                f"[runner] WARNING: CLI maintenance error ({type(exc).__name__}); "
                "existing capacity is unchanged.",
                flush=True,
            )
        interval = _env_float(
            "BLUEPRINT_RUNNER_CLI_MAINTENANCE_INTERVAL",
            float(_CLI_MAINTENANCE_INTERVAL_SECONDS),
            minimum=60.0,
        )
        if _shutdown_event.wait(interval):
            return


def _host_cli_maintenance_allowed(
    caps: "iso.Capabilities", agent_image: str | None
) -> bool:
    """Manage host CLIs whenever bwrap remains a selectable agent backend.

    A configured Docker image only covers the light read-only task slice. On a
    hybrid Docker+bwrap runner, write/heavy tasks still select bwrap and require
    healthy host CLIs. A genuinely Docker-only runner has no selectable host
    backend and remains image-managed.
    """
    del agent_image  # Image selection cannot remove the bwrap fallback slice.
    return caps.bwrap_ok


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


def _gpu_memory_mib() -> tuple[list[int], list[int]] | None:
    """Per-GPU (total, free) VRAM in MiB, best-effort/nullable.

    Queries ``nvidia-smi --query-gpu=memory.total,memory.free`` so dispatch and
    authoring can reason about VRAM fit (a ``has_gpu`` boolean cannot tell a
    16 GiB V100 from a 46 GiB L40S). Any failure (no GPU, no nvidia-smi,
    timeout, unparseable output) returns None and the heartbeat simply omits
    the fields — old-runner-identical behavior.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.free",
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
    totals: list[int] = []
    frees: list[int] = []
    for line in result.stdout.splitlines():
        parts = [tok.strip() for tok in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            total = int(float(parts[0]))
            free = int(float(parts[1]))
        except ValueError:
            continue
        totals.append(max(0, total))
        frees.append(max(0, free))
    if not totals:
        return None
    return totals, frees


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


def _memory_total_bytes() -> int | None:
    """Total host RAM in bytes, or ``None`` when the host cannot report it."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, FileNotFoundError, IndexError):
        pass
    return None


def _env_enabled(name: str, default: bool = True) -> bool:
    """Parse a conventional boolean env var without making startup fragile."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class _ResourceRequest:
    cpu_cores: float
    memory_bytes: int
    disk_bytes: int


class _ResourceLedger:
    """Single-daemon resource admission ledger shared by agent and compute pools.

    The existing per-pool semaphores remain useful policy ceilings, but cannot
    protect a host from ``agents + compute`` together exceeding its CPU or RAM.
    This ledger reserves a conservative, operator-configurable amount before the
    claim RPC. Reservations are keyed and released only when the corresponding
    task/job leaves the runner, so the two poll loops cannot race into admitting
    the same host capacity.
    """

    _GIB = 1024 * 1024 * 1024

    def __init__(
        self,
        workspace_root: str | None,
        *,
        enabled: bool | None = None,
        agent: _ResourceRequest | None = None,
        compute: _ResourceRequest | None = None,
        reserved_cpu_cores: float | None = None,
        reserved_memory_bytes: int | None = None,
        min_free_memory_bytes: int | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.enabled = (
            _env_enabled("BLUEPRINT_RUNNER_RESOURCE_ADMISSION", True)
            if enabled is None else enabled
        )
        self.agent = agent or _ResourceRequest(
            cpu_cores=_env_float("BLUEPRINT_RUNNER_AGENT_CPU_RESERVATION_CORES", 1.0, minimum=0.0),
            memory_bytes=_env_int("BLUEPRINT_RUNNER_AGENT_MEMORY_RESERVATION_BYTES", 2 * self._GIB, minimum=0),
            disk_bytes=_env_int("BLUEPRINT_RUNNER_AGENT_DISK_RESERVATION_BYTES", 0, minimum=0),
        )
        self.compute = compute or _ResourceRequest(
            cpu_cores=_env_float("BLUEPRINT_RUNNER_COMPUTE_CPU_RESERVATION_CORES", 2.0, minimum=0.0),
            memory_bytes=_env_int("BLUEPRINT_RUNNER_COMPUTE_MEMORY_RESERVATION_BYTES", 4 * self._GIB, minimum=0),
            disk_bytes=_env_int("BLUEPRINT_RUNNER_COMPUTE_DISK_RESERVATION_BYTES", 0, minimum=0),
        )
        # A one-vCPU host used to be a supported runner configuration.  Retain
        # that compatibility by not consuming its only core with the *default*
        # control-plane reserve.  An operator who explicitly sets the variable
        # still gets the exact requested fail-closed reservation.
        default_reserved_cpu = 0.0 if os.cpu_count() == 1 else 1.0
        self.reserved_cpu_cores = (
            _env_float("BLUEPRINT_RUNNER_RESERVED_CPU_CORES", default_reserved_cpu, minimum=0.0)
            if reserved_cpu_cores is None else max(0.0, reserved_cpu_cores)
        )
        self.reserved_memory_bytes = (
            _env_int("BLUEPRINT_RUNNER_RESERVED_MEMORY_BYTES", self._GIB, minimum=0)
            if reserved_memory_bytes is None else max(0, reserved_memory_bytes)
        )
        self.min_free_memory_bytes = (
            _env_int("BLUEPRINT_RUNNER_MIN_FREE_MEMORY_BYTES", self._GIB, minimum=0)
            if min_free_memory_bytes is None else max(0, min_free_memory_bytes)
        )
        self._lock = threading.Lock()
        self._reservations: dict[str, _ResourceRequest] = {}

    def request_for(self, workload: str) -> _ResourceRequest:
        return self.compute if workload == "compute" else self.agent

    def _reason_locked(self, request: _ResourceRequest) -> str | None:
        if not self.enabled:
            return None
        reserved_cpu = sum(item.cpu_cores for item in self._reservations.values())
        total_cpu = os.cpu_count()
        if (
            total_cpu is not None
            and reserved_cpu + request.cpu_cores + self.reserved_cpu_cores > total_cpu
        ):
            return "cpu_reservation"

        reserved_memory = sum(item.memory_bytes for item in self._reservations.values())
        total_memory = _memory_total_bytes()
        if (
            total_memory is not None
            and reserved_memory + request.memory_bytes + self.reserved_memory_bytes > total_memory
        ):
            return "memory_reservation"
        free_memory = _memory_free_bytes()
        if (
            free_memory is not None
            and free_memory < self.min_free_memory_bytes + reserved_memory + request.memory_bytes
        ):
            return "memory_pressure"

        reserved_disk = sum(item.disk_bytes for item in self._reservations.values())
        min_disk = _min_free_disk_bytes()
        free_disk = _disk_free_bytes(self.workspace_root)
        if (
            free_disk is not None
            and free_disk < min_disk + reserved_disk + request.disk_bytes
        ):
            return "disk_pressure"
        return None

    def try_reserve(self, key: str, workload: str) -> str | None:
        """Atomically reserve one workload slot; return a rejection reason or None."""
        request = self.request_for(workload)
        with self._lock:
            if key in self._reservations:
                return None
            reason = self._reason_locked(request)
            if reason is not None:
                return reason
            self._reservations[key] = request
            return None

    def rename(self, old_key: str, new_key: str) -> None:
        """Transfer a pre-claim permit to its durable task/job identity."""
        with self._lock:
            request = self._reservations.pop(old_key, None)
            if request is not None:
                self._reservations[new_key] = request

    def release(self, key: str) -> None:
        with self._lock:
            self._reservations.pop(key, None)

    def snapshot(self, gpu_state: dict | None = None) -> dict:
        """Heartbeat-safe capacity report. Physical probes remain best effort."""
        with self._lock:
            reserved_cpu = round(sum(item.cpu_cores for item in self._reservations.values()), 3)
            reserved_memory = sum(item.memory_bytes for item in self._reservations.values())
            reserved_disk = sum(item.disk_bytes for item in self._reservations.values())
            reservations = len(self._reservations)
        total_cpu = os.cpu_count()
        total_memory = _memory_total_bytes()
        free_memory = _memory_free_bytes()
        total_disk = free_disk = None
        if self.workspace_root:
            try:
                usage = shutil.disk_usage(self.workspace_root)
                total_disk, free_disk = int(usage.total), int(usage.free)
            except (OSError, ValueError):
                pass
        if gpu_state:
            with gpu_state["lock"]:
                gpu_total = len(gpu_state.get("allowed") or [])
                gpu_reserved = len(gpu_state.get("in_use") or [])
        else:
            gpu_total = gpu_reserved = 0
        return {
            "enabled": self.enabled,
            "reservations": reservations,
            "cpu": {
                "total_cores": total_cpu,
                "reserved_cores": reserved_cpu,
                "host_reserved_cores": self.reserved_cpu_cores,
                "free_cores": (
                    max(0.0, round(total_cpu - self.reserved_cpu_cores - reserved_cpu, 3))
                    if total_cpu is not None else None
                ),
            },
            "memory": {
                "total_bytes": total_memory,
                "reserved_bytes": reserved_memory,
                "host_reserved_bytes": self.reserved_memory_bytes,
                "observed_free_bytes": free_memory,
                "free_bytes": _available_memory_capacity_bytes(
                    total_memory=total_memory,
                    observed_free_memory=free_memory,
                    host_reserved_memory=self.reserved_memory_bytes,
                    workload_reserved_memory=reserved_memory,
                    min_free_memory=self.min_free_memory_bytes,
                ),
                "min_free_bytes": self.min_free_memory_bytes,
            },
            "disk": {
                "total_bytes": total_disk,
                "reserved_bytes": reserved_disk,
                "observed_free_bytes": free_disk,
                "free_bytes": (
                    max(0, free_disk - _min_free_disk_bytes() - reserved_disk)
                    if free_disk is not None else None
                ),
                "min_free_bytes": _min_free_disk_bytes(),
            },
            "gpu": {
                "total": gpu_total,
                "reserved": gpu_reserved,
                "free": max(0, gpu_total - gpu_reserved),
            },
        }


def _available_memory_capacity_bytes(
    *,
    total_memory: int | None,
    observed_free_memory: int | None,
    host_reserved_memory: int,
    workload_reserved_memory: int,
    min_free_memory: int,
) -> int | None:
    """Return the conservative memory admission headroom used by the ledger."""
    candidates: list[int] = []
    if total_memory is not None:
        candidates.append(total_memory - host_reserved_memory - workload_reserved_memory)
    if observed_free_memory is not None:
        candidates.append(observed_free_memory - min_free_memory - workload_reserved_memory)
    return max(0, min(candidates)) if candidates else None


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
        # Last best-effort Docker image-store reclaim. This is separate from job
        # health so an operator can see that a blocked container job triggered a
        # cleanup attempt even when no job was claimed.
        self._last_container_image_reclaim: dict | None = None
        self._pause_start_at: float | None = None

    def record_success(self) -> None:
        with self._lock:
            self.last_successful_job_at = _utc_now_iso()
            self.consecutive_job_failures = 0
            self._pause_start_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_job_failures += 1

    def record_container_image_reclaim(self, store: dict, result: dict) -> None:
        """Record a bounded image-store cleanup result for the next heartbeat."""
        root = store.get("root")
        after = _disk_free_bytes(root) if isinstance(root, str) else None
        reason = result.get("reason") or result.get("output")
        with self._lock:
            self._last_container_image_reclaim = {
                "attempted_at": _utc_now_iso(),
                "runtime": store.get("runtime"),
                "store_root": root,
                "free_bytes_before": store.get("free_bytes"),
                "free_bytes_after": after,
                "removed": result.get("removed"),
                "kept": result.get("kept"),
                "in_use_skipped": result.get("in_use_skipped"),
                "reason": str(reason)[:500] if reason else None,
            }

    def container_image_reclaim_snapshot(self) -> dict | None:
        with self._lock:
            return (
                dict(self._last_container_image_reclaim)
                if self._last_container_image_reclaim is not None
                else None
            )

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


# Must match the server-side container-image admission default. This is
# intentionally independent from BLUEPRINT_RUNNER_MIN_DISK_BYTES: operators can
# leave bwrap/imageless workspace work enabled below this level while Docker
# image-store recovery still runs before a container pull is admitted.
_CONTAINER_IMAGE_RECLAIM_MIN_FREE_BYTES_DEFAULT = 10_737_418_240  # 10 GiB


def _container_image_reclaim_min_free_bytes() -> int:
    """Hard cleanup floor for Docker image storage (never below 10 GiB)."""
    raw = os.environ.get("BLUEPRINT_RUNNER_CONTAINER_IMAGE_MIN_DISK_BYTES", "").strip()
    try:
        configured = int(raw) if raw else 0
    except ValueError:
        configured = 0
    return max(
        _CONTAINER_IMAGE_RECLAIM_MIN_FREE_BYTES_DEFAULT,
        _min_free_disk_bytes(),
        configured,
    )


def _disk_below_min(root: str | None, min_free_bytes: int) -> bool:
    """True iff *root*'s free space is known and below *min_free_bytes* (>0)."""
    if min_free_bytes <= 0:
        return False
    free = _disk_free_bytes(root)
    return free is not None and free < min_free_bytes


def _container_image_store(
    caps: "iso.Capabilities | None", *, _run=subprocess.run
) -> dict | None:
    """Describe the local container image store, including its own free space.

    Container layers are written to a runtime-managed data root, not necessarily
    to the runner workspace. Keep the measurements separate: compute scratch,
    clones, and artifacts need the workspace filesystem; a container image pull
    needs the image-store filesystem. An unknown store remains ``None`` so an
    older/remote/misconfigured runtime does not create a false disk gate.

    Docker is the common case (``DockerRootDir``); podman uses its graph root.
    Other container runtimes are intentionally not guessed here because their
    storage layouts are deployment-specific.
    """
    if caps is None:
        return None
    runtime = caps.preferred_container()
    if runtime == iso.BACKEND_DOCKER:
        argv = ["docker", "info", "--format", "{{.DockerRootDir}}"]
    elif runtime == iso.BACKEND_PODMAN:
        argv = ["podman", "info", "--format", "{{.Store.GraphRoot}}"]
    else:
        return None
    try:
        result = _run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    root = (result.stdout or "").strip()
    if result.returncode != 0 or not root or not os.path.isabs(root):
        return None
    return {
        "runtime": runtime,
        "root": root,
        "free_bytes": _disk_free_bytes(root),
    }


def _compute_image_keep_days() -> int:
    """Days of LAST-USE recency that protect a Docker image from cache hygiene.

    Configurable via ``BLUEPRINT_RUNNER_IMAGE_KEEP_DAYS`` (default 7). An image
    used or pulled within this window — and any image backing a container — is
    never pruned (see :func:`_prune_docker_images`; the window is measured
    against last-use/pull, NOT the image's upstream creation date). The dgx-1 has
    ~30 GB of image-layer storage, so unbounded growth of per-project compute
    images would eventually wedge the runner; this bounds it without evicting
    images that a recent or in-flight job still needs.
    """
    raw = os.environ.get("BLUEPRINT_RUNNER_IMAGE_KEEP_DAYS", "").strip()
    if not raw:
        return 7
    try:
        return max(0, int(raw))
    except ValueError:
        return 7


def _image_use_ledger_path() -> str:
    """Path of the runner's image last-use ledger (``~/.blueprint/image_use.json``)."""
    return os.path.join(_credential_dir(), "image_use.json")


def _load_image_use_ledger() -> dict:
    """The runner's image last-use ledger. Best-effort — {} on any read error.

    Each value is EITHER a bare ISO-8601 last-use string (legacy shape) OR a
    ``{"last_used": iso8601, "project_id": str}`` object once the runner learns
    which project drove the pull. Both shapes coexist on disk across a rolling
    upgrade, so every reader goes through :func:`_ledger_last_used` /
    :func:`_ledger_project_id` rather than touching the raw value.
    """
    try:
        with open(_image_use_ledger_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ledger_last_used(value) -> str | None:
    """Extract the ISO-8601 last-use string from a ledger value (either shape)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        got = value.get("last_used")
        return got if isinstance(got, str) else None
    return None


def _ledger_project_id(value) -> str | None:
    """Extract the owning project id from a ledger value, or None (legacy shape)."""
    if isinstance(value, dict):
        pid = value.get("project_id")
        return pid if isinstance(pid, str) and pid else None
    return None


def _record_image_use(image_ref: str | None, *, project_id: str | None = None, _now=None) -> None:
    """Stamp *image_ref* as used NOW in the last-use ledger (best-effort).

    Docker records no last-use time for an image, so the runner keeps its own.
    Without this, cache hygiene can only see image CREATION time — which says
    nothing about whether a job needs the image (see :func:`_prune_docker_images`).

    When *project_id* is known (a multi-project runner serves several projects
    from one image cache), it is stored alongside the timestamp so the heartbeat
    can attribute per-image storage back to the project that pulled it
    (see :func:`_docker_image_cache_by_project`). An owning project already on
    record is preserved when a later use arrives without one, so a re-pull under
    a stale runner never erases attribution.
    """
    if not image_ref:
        return
    now = _now or datetime.now(timezone.utc)
    try:
        ledger = _load_image_use_ledger()
        owner = project_id or _ledger_project_id(ledger.get(image_ref))
        stamp = now.isoformat()
        ledger[image_ref] = (
            {"last_used": stamp, "project_id": owner} if owner else stamp
        )
        # Bound the ledger: drop stamps older than 90 days so it cannot grow
        # without limit on a long-lived runner.
        floor = now - timedelta(days=90)
        pruned = {
            ref: ts
            for ref, ts in ledger.items()
            if ref == image_ref
            or ((_parse_docker_time(_ledger_last_used(ts)) or now) >= floor)
        }
        path = _image_use_ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh)
        os.replace(tmp_path, path)
    except Exception:
        pass  # cache bookkeeping must never break a job


def _parse_docker_time(raw: str | None) -> datetime | None:
    """Parse a Docker/Go timestamp into an aware UTC datetime, else None.

    Handles the three shapes docker emits across versions/fields:
      * RFC3339 (``.Created``):            ``2026-07-01T12:34:56.789012345Z``
      * Go ``time.Time`` (``.Metadata.LastTagTime``):
                                           ``2026-07-01 12:34:56.789 +0000 UTC``
      * the Go zero value (never tagged locally) → None, not year 1.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    # Go time.Time default rendering ("2026-07-01 12:34:56.789 +0000 UTC"):
    # date and time are space-separated. Rebuild it as ISO and drop the trailing
    # zone NAME (keeping the numeric offset). NB: the zone name itself contains a
    # "T" ("UTC"), so the separator position — not a naive "T" scan — is the test.
    if len(text) > 10 and text[10] == " ":
        parts = text.split()
        if len(parts) >= 3:
            text = f"{parts[0]}T{parts[1]}{parts[2]}"
        elif len(parts) == 2:
            text = f"{parts[0]}T{parts[1]}"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python's fromisoformat rejects >6 fractional digits (docker emits 9).
    if "." in text:
        head, _, tail = text.partition(".")
        frac = ""
        idx = 0
        while idx < len(tail) and tail[idx].isdigit():
            frac += tail[idx]
            idx += 1
        text = f"{head}.{frac[:6]}{tail[idx:]}"
    # Normalize a compact UTC offset (+0000) to the ISO form (+00:00).
    if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_repo_tags(raw: str) -> list[str]:
    """Parse a Go-rendered RepoTags slice (``[a:1 b:2]``) into a list."""
    text = (raw or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [t for t in text.split() if t and t != "<none>:<none>"]


def _prune_docker_images(
    workspace_root: str | None,
    min_free_bytes: int,
    *,
    keep_days: int | None = None,
    _run=subprocess.run,
    _disk_below=None,
    _now=None,
    _ledger=None,
    _protected=None,
) -> dict:
    """Best-effort: reclaim Docker image storage when disk runs low.

    Runs ONLY when *workspace_root*'s free space is below *min_free_bytes* — the
    same guard the agent-claim path uses (:func:`_disk_below_min`).

    Selection keys on LAST-USE / PULL recency, never on image CREATION time.
    ``docker image prune --filter until=<age>`` (what this used to call) keys on
    the image's *creation* timestamp — baked into the image at BUILD time by the
    registry. A CUDA base image published upstream months ago is "older than the
    keep window" the instant it lands on disk, so a just-pulled, actively-needed
    multi-GB image was deleted immediately and re-pulled by the very next job:
    the hygiene pass thrashed the cache it exists to protect.

    An image is KEPT when any of these is within the keep window:
      * the runner's own last-use stamp (:func:`_record_image_use`) — the true
        "a job needed this image" signal;
      * ``Metadata.LastTagTime`` — when docker last tagged it locally, i.e. the
        PULL time for registry images;
      * ``.Created`` — only as a floor for locally-built images that were never
        tagged (there, build time IS local recency).
    It is also kept when it is the configured agent image, or when it is
    referenced by any container (``docker image rm`` without ``-f`` refuses to
    delete those, so in-use protection is enforced by docker itself).

    Returns a status dict for logging/testing. Never raises — a missing ``docker``
    binary or a docker error degrades to ``{"pruned": False, ...}``.
    """
    keep = _compute_image_keep_days() if keep_days is None else keep_days
    disk_below = _disk_below if _disk_below is not None else _disk_below_min
    if not disk_below(workspace_root, min_free_bytes):
        return {"pruned": False, "reason": "disk_above_threshold"}

    now = _now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(0, keep))
    ledger = _load_image_use_ledger() if _ledger is None else dict(_ledger)
    if _protected is None:
        protected_refs = {
            r for r in [(os.environ.get("BLUEPRINT_AGENT_IMAGE") or "").strip()] if r
        }
    else:
        protected_refs = set(_protected)

    try:
        listing = _run(
            ["docker", "image", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"pruned": False, "reason": f"prune_error:{type(e).__name__}"}
    if listing.returncode != 0:
        return {
            "pruned": False,
            "reason": "prune_nonzero",
            "stderr": (listing.stderr or "")[-500:],
        }
    image_ids = sorted({ln.strip() for ln in (listing.stdout or "").splitlines() if ln.strip()})
    if not image_ids:
        return {"pruned": True, "keep_days": keep, "removed": 0, "kept": 0,
                "removed_ids": [], "in_use_skipped": 0, "output": "no images"}

    # Local recency of every image: last local tag (== pull time for registry
    # images), with creation time only as a floor for never-tagged local builds.
    try:
        inspected = _run(
            [
                "docker", "image", "inspect", "--format",
                "{{.Id}}\t{{.Metadata.LastTagTime}}\t{{.Created}}\t{{.RepoTags}}",
                *image_ids,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"pruned": False, "reason": f"prune_error:{type(e).__name__}"}
    if inspected.returncode != 0:
        return {
            "pruned": False,
            "reason": "prune_nonzero",
            "stderr": (inspected.stderr or "")[-500:],
        }

    stale: list[str] = []
    kept = 0
    for line in (inspected.stdout or "").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        image_id = fields[0].strip()
        tag_time = _parse_docker_time(fields[1])
        created = _parse_docker_time(fields[2])
        repo_tags = _parse_repo_tags(fields[3] if len(fields) > 3 else "")

        if protected_refs.intersection(repo_tags):
            kept += 1
            continue

        candidates = [t for t in (tag_time, created) if t is not None]
        for ref in repo_tags:
            used = _parse_docker_time(_ledger_last_used(ledger.get(ref)))
            if used is not None:
                candidates.append(used)
        last_used = max(candidates) if candidates else None
        # Undatable image: keep it. Never delete what we cannot age honestly.
        if last_used is None or last_used >= cutoff:
            kept += 1
            continue
        stale.append(image_id)

    removed: list[str] = []
    in_use_skipped = 0
    for image_id in stale:
        try:
            # No `-f`: docker refuses to delete an image referenced by a running
            # OR stopped container, which is exactly the protection we want.
            rm = _run(
                ["docker", "image", "rm", image_id],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            in_use_skipped += 1
            continue
        if rm.returncode == 0:
            removed.append(image_id)
        else:
            in_use_skipped += 1

    return {
        "pruned": True,
        "keep_days": keep,
        "removed": len(removed),
        "removed_ids": removed,
        "kept": kept,
        "in_use_skipped": in_use_skipped,
        "output": f"removed={len(removed)} kept={kept} in_use_or_error={in_use_skipped}",
    }


# Cap on images accounted per heartbeat. A pathological cache can't turn the
# telemetry pass into an unbounded inspect; excess images fall into the total
# only (via the cheap `ls` size column), never the per-image inspect loop.
_IMAGE_CACHE_MAX_IMAGES = 500


def _docker_image_cache_by_project(*, _run=subprocess.run, _ledger=None) -> dict | None:
    """Best-effort per-project Docker image-cache accounting for the heartbeat.

    A local-runner daemon can serve several projects from ONE docker image
    cache (the project allow-list), but nothing on the runner attributed image
    storage to a project — the heartbeat reported only whole-filesystem
    ``disk_free_bytes``. On a small data-root (dgx-1 is ~30 GB of image-layer
    storage) an operator could not see WHICH project's images were filling the
    disk. This walks ``docker image inspect`` once and attributes each image's
    size to the project that last pulled/used it, per the last-use ledger
    (:func:`_record_image_use`).

    Returns None when docker is unavailable or errors (the field is then simply
    omitted from the heartbeat — older/dockerless runners are unaffected).
    Otherwise::

        {
          "total_bytes": int,            # Σ per-image virtual sizes (see NB)
          "image_count": int,
          "by_project": {project_id: {"bytes": int, "image_count": int}},
          "unattributed_bytes": int,     # images with no ledger owner yet
          "unattributed_image_count": int,
        }

    NB: image sizes are per-image VIRTUAL sizes; two images sharing a base
    layer each count the shared bytes, so ``total_bytes`` over-counts a cache
    with heavy layer reuse. This is attribution telemetry, not an exact disk
    measurement — ``disk_free_bytes`` remains the source of truth that gates
    the low-disk prune. Best-effort throughout: never raises.
    """
    ledger = _load_image_use_ledger() if _ledger is None else dict(_ledger)
    try:
        listing = _run(
            ["docker", "image", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    image_ids = sorted({ln.strip() for ln in (listing.stdout or "").splitlines() if ln.strip()})
    if not image_ids:
        return {
            "total_bytes": 0, "image_count": 0, "by_project": {},
            "unattributed_bytes": 0, "unattributed_image_count": 0,
        }

    try:
        inspected = _run(
            [
                "docker", "image", "inspect", "--format",
                "{{.Id}}\t{{.Size}}\t{{.RepoTags}}",
                *image_ids[:_IMAGE_CACHE_MAX_IMAGES],
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if inspected.returncode != 0:
        return None

    total = 0
    image_count = 0
    by_project: dict[str, dict] = {}
    unattributed_bytes = 0
    unattributed_count = 0
    for line in (inspected.stdout or "").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        try:
            size = max(0, int((fields[1] or "0").strip()))
        except ValueError:
            continue
        repo_tags = _parse_repo_tags(fields[2] if len(fields) > 2 else "")
        owner = None
        for ref in repo_tags:
            owner = _ledger_project_id(ledger.get(ref))
            if owner:
                break
        total += size
        image_count += 1
        if owner:
            bucket = by_project.setdefault(owner, {"bytes": 0, "image_count": 0})
            bucket["bytes"] += size
            bucket["image_count"] += 1
        else:
            unattributed_bytes += size
            unattributed_count += 1

    return {
        "total_bytes": total,
        "image_count": image_count,
        "by_project": by_project,
        "unattributed_bytes": unattributed_bytes,
        "unattributed_image_count": unattributed_count,
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
# Compatibility ceiling for the legacy orchestrator-proxied multipart route.
# Production-size bundles use the direct transfer broker and have no runner cap.
_RUNNER_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB
_ARTIFACT_TRANSFER_CHUNK_BYTES = 1024 * 1024
_ARTIFACT_DELIVERY_REASON_MAX_CHARS = 256

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

# Interim-progress protocol (BLUEPRINT_PROGRESS_FILE). The experiment appends
# JSONL records like {"step": int, "metric": str, "value": float, "ts": "..."};
# the runner tails the file between report heartbeats and attaches a bounded
# tail to its interim (exec_status="running") compute-job reports. Everything
# is bounded: records kept, bytes serialized per report, bytes read per poll,
# and bytes tolerated for a single (possibly hostile) line.
_PROGRESS_TAIL_MAX_RECORDS = 20
_PROGRESS_TAIL_MAX_BYTES = 2048
_PROGRESS_READ_CHUNK_BYTES = 262144
_PROGRESS_MAX_LINE_BYTES = 8192
_PROGRESS_METRIC_MAX_CHARS = 128
_PROGRESS_TS_MAX_CHARS = 64
# Interim command-log tail attached to running reports so operators see live
# output mid-run (stamped server-side as polling_metadata["live_output"]).
_RUNNER_LIVE_TAIL_CHARS = 4096

# ── Mid-run viewable-artifact streaming ──────────────────────────────────────
# WHILE a compute job runs, the runner scans $BLUEPRINT_ARTIFACT_DIR for viewable
# files (reward-curve plots, rollout frames/videos) and pushes NEW-or-CHANGED
# ones to POST /api/runner/compute-job/{job_id}/viewable so they render live in
# the strategic canvas instead of only at job completion. The extension allowlist,
# per-kind byte caps, and 25-file budget MIRROR the server's (app.storage
# ._VIEWABLE_CONTENT_TYPES + the /viewable endpoint) so the runner does not waste
# an upload the server would reject: png/jpg/jpeg/webp/gif images ≤ 8 MiB, mp4/webm
# videos ≤ 512 MiB, at most 25 distinct viewable objects per job. SVG is sanitized
# server-side (script tags stripped) before being stored and served.
_VIEWABLE_CONTENT_TYPES = {
    "png": ("image/png", "image"),
    "jpg": ("image/jpeg", "image"),
    "jpeg": ("image/jpeg", "image"),
    "webp": ("image/webp", "image"),
    "gif": ("image/gif", "image"),
    "mp4": ("video/mp4", "video"),
    "webm": ("video/webm", "video"),
    "pdf": ("application/pdf", "pdf"),
    "svg": ("image/svg+xml", "image"),
}
_MAX_VIEWABLE_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MiB (server caps images here)
_MAX_VIEWABLE_VIDEO_BYTES = int(
    os.environ.get("BLUEPRINT_MAX_VIEWABLE_VIDEO_BYTES", str(512 * 1024 * 1024))
)  # mirrors backend/app/compute_backends.py BLUEPRINT_MAX_VIEWABLE_VIDEO_BYTES
_MAX_VIEWABLE_FILE_COUNT = 25  # at most N individually-addressable objects/job
_FINAL_VIEWABLE_DRAIN_SECONDS = 10.0
# Maximum total directory entries (files + dirs) inspected per scan cycle.
# Without a cap, a task that writes thousands of non-viewable files (checkpoints,
# logs) causes an unbounded os.walk on every 30-second heartbeat, blocking I/O
# proportional to the directory size rather than the 25-file viewable budget.
_VIEWABLE_SCAN_FILE_CAP = 10_000


def _report_retry_sleep(attempt: int) -> float:
    """Capped exponential backoff (seconds) for the terminal-report retry loop."""
    return min(_REPORT_RETRY_BASE_SLEEP * (2 ** attempt), _REPORT_RETRY_MAX_SLEEP)


def _parse_progress_record(line: bytes) -> dict | None:
    """Parse+validate one BLUEPRINT_PROGRESS_FILE JSONL line, or None.

    The file is written by sandboxed experiment code, so every field is
    validated before it leaves the runner: ``metric`` must be a non-empty
    string (bounded), ``value`` a finite number; ``step`` (non-negative int)
    and ``ts`` (bounded string) are optional. Anything else — garbage bytes,
    non-dict JSON, NaN/inf, absurd types — is dropped, never raised.
    """
    text = line.strip()
    if not text or len(text) > _PROGRESS_MAX_LINE_BYTES:
        return None
    try:
        raw = json.loads(text.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    metric = raw.get("metric")
    value = raw.get("value")
    if not isinstance(metric, str) or not metric.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return None
    record: dict = {"metric": metric.strip()[:_PROGRESS_METRIC_MAX_CHARS], "value": numeric}
    step = raw.get("step")
    if isinstance(step, int) and not isinstance(step, bool) and 0 <= step <= 10**12:
        record["step"] = step
    ts = raw.get("ts")
    if isinstance(ts, str) and ts:
        record["ts"] = ts[:_PROGRESS_TS_MAX_CHARS]
    return record


class _ProgressTail:
    """Offset-tracked, bounded reader of a job's BLUEPRINT_PROGRESS_FILE.

    ``poll()`` reads only the bytes appended since the previous poll (capped at
    ``_PROGRESS_READ_CHUNK_BYTES`` per call), parses complete JSONL lines, and
    keeps the last ``_PROGRESS_TAIL_MAX_RECORDS`` valid records plus an exact
    running count. A partial trailing line is carried to the next poll; an
    oversized line (> ``_PROGRESS_MAX_LINE_BYTES``, e.g. hostile output) is
    discarded up to its next newline so the reader can never wedge or balloon.
    A file that shrinks below the tracked offset (truncated/rewritten by the
    job) resets the reader to the top instead of freezing at a dead offset.

    ``snapshot()`` returns the report payload ``{"records": [...], "count": n}``
    (records byte-bounded to ``_PROGRESS_TAIL_MAX_BYTES``) or None when nothing
    valid has been observed — an absent field on the wire, so old backends see
    byte-identical reports for progress-less jobs.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._offset = 0
        self._count = 0
        self._records: deque = deque(maxlen=_PROGRESS_TAIL_MAX_RECORDS)
        self._partial = b""
        self._skip_to_newline = False

    def poll(self) -> dict | None:
        """Consume newly-appended bytes and return the current snapshot."""
        try:
            with open(self._path, "rb") as f:
                size = f.seek(0, io.SEEK_END)
                if size < self._offset:
                    # File was truncated/rewritten under us (e.g. the job
                    # recreates its progress file). Restart from the top —
                    # the stale partial-line carry belongs to bytes that no
                    # longer exist — so progress resumes instead of freezing.
                    self._offset = 0
                    self._partial = b""
                    self._skip_to_newline = False
                f.seek(self._offset)
                chunk = f.read(_PROGRESS_READ_CHUNK_BYTES)
        except OSError:
            return self.snapshot()
        if chunk:
            self._offset += len(chunk)
            data = self._partial + chunk
            lines = data.split(b"\n")
            # Last element is the (possibly empty) partial line after the final
            # newline — carry it forward, bounded.
            self._partial = lines.pop()
            for line in lines:
                if self._skip_to_newline:
                    # This newline terminates the oversized line being skipped.
                    self._skip_to_newline = False
                    continue
                record = _parse_progress_record(line)
                if record is not None:
                    self._count += 1
                    self._records.append(record)
            if len(self._partial) > _PROGRESS_MAX_LINE_BYTES:
                # A single line larger than the cap: drop what we have and skip
                # the remainder until its newline shows up in a later poll.
                self._partial = b""
                self._skip_to_newline = True
        return self.snapshot()

    def snapshot(self) -> dict | None:
        if self._count <= 0:
            return None
        records = list(self._records)
        while len(records) > 1 and len(json.dumps(records)) > _PROGRESS_TAIL_MAX_BYTES:
            records.pop(0)
        return {"records": records, "count": self._count}


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


def _notify_decline(
    orchestrator_url: str,
    token: str,
    task_id: str,
    reason: str | None = None,
) -> None:
    """Best-effort: tell the orchestrator this runner declined a claimed task.

    When the runner claims a task from /next-task but then declines it at the
    isolation-backend level (no bwrap, no docker, capability mismatch), the
    orchestrator still has it pinned in PROVISIONING.  Without this notification
    the task strands until the stale-runner sweep fires when THIS runner goes
    offline — which may be hours or never.  This call releases the pin so the
    dispatch loop can re-pin it next cycle.

    ``reason`` carries WHY we declined (the sandbox/spawn failure string we
    already print locally).  The orchestrator records it on the task and excludes
    this runner from re-selection, which is what stops the pin→decline loop; it
    is optional purely so an older orchestrator still accepts the call.
    """
    try:
        url = f"{orchestrator_url}/api/runner/decline-task"
        body = {"task_id": task_id}
        if reason:
            body["reason"] = reason[:500]
        _post(url, token, body)
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
    resource_ledger: "_ResourceLedger | None" = None,
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
                    # Release the durable resource permit while this task ID is
                    # still visible as active.  Otherwise a fresh dispatch of
                    # the same ID could install a new permit between `del` and
                    # release, and the old reaper would remove the new permit.
                    if resource_ledger is not None:
                        resource_ledger.release(f"agent:{tid}")
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
                        if resource_ledger is not None:
                            resource_ledger.release(f"agent:{tid}")
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


# ── Live-safe config: server-authoritative reconfiguration via heartbeat ──────

class _LiveRunnerConfig:
    """Thread-safe holder for the LIVE-SAFE runner config subset.

    The heartbeat RESPONSE carries a server-authoritative ``config`` block
    (``max_concurrent`` / ``max_concurrent_compute`` / ``task_types`` /
    ``gpu_indices_allowed`` plus a ``config_generation`` marker). The env-var
    startup values seed this holder as the INITIAL defaults; a server config
    block OVERRIDES them live. The agent and compute claim loops read
    ``max_concurrent()`` / ``max_concurrent_compute()`` each iteration, so
    GROWING the limit lets new claims proceed on the next tick and SHRINKING it
    simply stops NEW claims — running work is never touched. The daemon echoes
    the applied generation + values back on the next heartbeat so the
    orchestrator/UI can render desired-vs-applied (``config_pending``).
    """

    # Concurrency bounds the server validates (backend runner_config). A live
    # update is clamped to this range so a bad push can never wedge the runner
    # idle (< 1) or blow the pool past the ceiling (> 64).
    MIN_CONCURRENT = 1
    MAX_CONCURRENT = 64

    # The live-safe keys this daemon knows how to apply mid-flight. A config
    # block carrying an UNRECOGNIZED non-null field is treated as
    # not-fully-applicable: the daemon leaves the new generation UNAPPLIED and
    # keeps reporting the older one, so the server renders it as pending rather
    # than the daemon silently dropping a field it does not understand.
    _KNOWN_KEYS = frozenset(
        {
            "max_concurrent",
            "max_concurrent_compute",
            "task_types",
            "gpu_indices_allowed",
            "gpus_per_job",
        }
    )

    # Per-job GPU-count bounds, mirroring the server projection (runner_config).
    # A live push is clamped into this range so a bad value can never wedge the
    # scheduler (< 1) or reserve an absurd number of devices per job.
    MIN_GPUS_PER_JOB = 1
    MAX_GPUS_PER_JOB = 8

    def __init__(
        self,
        max_concurrent: int,
        max_concurrent_compute: int,
        task_types="all",
        gpu_state: dict | None = None,
        all_gpu_indices=None,
    ) -> None:
        self._lock = threading.Lock()
        self._max_concurrent = self._clamp(max_concurrent)
        self._max_concurrent_compute = self._clamp(max_concurrent_compute)
        self._task_types = task_types  # "all" or list[str]
        # Reported live-safe GPU allowlist value: "all" sentinel or a sorted
        # list of indices (mirrors the server's projected representation so the
        # echoed generation matches without recomputing the hash).
        self._gpu_indices_allowed = "all"
        # Shared GPU-scheduling dict; the concrete allowlist lives under
        # ["allowed"] and is read by _assign_gpus under ["lock"]. The per-job
        # GPU count lives under ["per_job"] and is read by _assign_gpus too, so a
        # live update to it takes effect on the NEXT job assignment.
        self._gpu_state = gpu_state
        # Reported per-job GPU count, seeded from the startup gpu_state value so
        # the echoed report matches the count the scheduler actually uses.
        self._gpus_per_job = self._clamp_gpus_per_job(
            (gpu_state or {}).get("per_job", self.MIN_GPUS_PER_JOB)
        )
        self._all_gpu_indices = list(all_gpu_indices or [])
        # None until the first server generation is applied; the server treats a
        # null applied generation as pending/unknown (never an error).
        self._applied_generation: str | None = None
        # Structured rejection state: when a config block cannot be fully applied
        # (an unrecognized non-null field), record WHY (naming the offending
        # field(s)), the offending field name(s) themselves, and WHICH generation
        # was rejected so the server/UI can render an honest "config rejected —
        # <reason>" badge instead of the ambiguous "rejected it or is stuck".
        # Cleared once a generation applies fully.
        self._rejection_reason: str | None = None
        self._rejected_fields: list[str] | None = None
        self._rejected_generation: str | None = None

    @classmethod
    def _clamp(cls, value) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return cls.MIN_CONCURRENT
        return max(cls.MIN_CONCURRENT, min(cls.MAX_CONCURRENT, v))

    @classmethod
    def _clamp_gpus_per_job(cls, value) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return cls.MIN_GPUS_PER_JOB
        return max(cls.MIN_GPUS_PER_JOB, min(cls.MAX_GPUS_PER_JOB, v))

    def max_concurrent(self) -> int:
        with self._lock:
            return self._max_concurrent

    def max_concurrent_compute(self) -> int:
        with self._lock:
            return self._max_concurrent_compute

    def report(self) -> dict:
        """Applied-vs-pending report fields for the next HeartbeatRequest body."""
        with self._lock:
            return {
                "applied_config_generation": self._applied_generation,
                "applied_max_concurrent": self._max_concurrent,
                "applied_max_concurrent_compute": self._max_concurrent_compute,
                "applied_task_types": self._task_types,
                "applied_gpu_indices_allowed": self._gpu_indices_allowed,
                "applied_gpus_per_job": self._gpus_per_job,
                # Structured apply outcome (additive): "applied" when the last
                # delivered generation was fully applied, "rejected" when a field
                # could not be applied. config_apply_reason names the offending
                # field(s); config_rejected_fields is the offending field name(s)
                # as a structured list; config_rejected_generation is the
                # generation that was rejected so the server can tell a current
                # failure from a stale one tied to a superseded generation. All
                # None on a clean apply.
                "config_apply_status": (
                    "rejected" if self._rejection_reason is not None else "applied"
                ),
                "config_apply_reason": self._rejection_reason,
                "config_rejected_fields": (
                    list(self._rejected_fields)
                    if self._rejected_fields is not None
                    else None
                ),
                "config_rejected_generation": self._rejected_generation,
            }

    @staticmethod
    def _parses_to_int(value) -> bool:
        """True when ``int(value)`` would succeed (well-typed for a clamp field).

        A well-typed-but-out-of-range int is still valid here — the field-level
        clamp is the server-bound defense for those. Only values ``int()``
        cannot parse (wrong type, unparseable string) are malformed.
        """
        try:
            int(value)
        except (TypeError, ValueError):
            return False
        return True

    def _classify_fields(self, config) -> tuple[list[str], list[str]]:
        """Validation pass (no lock, no mutation): classify each delivered field.

        Returns ``(unrecognized, malformed)`` — non-null keys the daemon does
        not know, and recognized keys whose value has the wrong type/shape to be
        applied (would otherwise be silently clamped-to-default or ignored while
        the generation was still claimed applied). ``None`` values are always
        well-formed no-ops. A generation is fully applicable only when BOTH
        lists are empty.
        """
        unrecognized: list[str] = []
        malformed: list[str] = []
        for key, value in config.items():
            if key == "config_generation":
                continue
            if key not in self._KNOWN_KEYS:
                if value is not None:
                    unrecognized.append(str(key))
                continue
            # Recognized key. A null value is a no-op (never malformed).
            if value is None:
                continue
            if key in ("max_concurrent", "max_concurrent_compute", "gpus_per_job"):
                if not self._parses_to_int(value):
                    malformed.append(key)
            elif key in ("task_types", "gpu_indices_allowed"):
                # Accepted shapes: the "all" sentinel or a list. Anything else
                # (a bare int/string/dict) would be silently dropped today.
                if value != "all" and not isinstance(value, list):
                    malformed.append(key)
        return unrecognized, malformed

    def apply(self, config) -> bool:
        """Reconcile a server config block onto the live process, atomically.

        Returns True when every delivered field is recognized AND well-formed:
        the whole block applies and the generation advances. Returns False when
        ANY field is unrecognized or malformed — in that case NOTHING is
        mutated (the generation is rejected as a unit, not partially applied),
        the older applied generation keeps being reported, and a structured
        rejection reason naming the offending field(s) is recorded. Never
        raises — a malformed/absent block leaves live config untouched and
        reports False.

        A well-typed-but-out-of-range clamp field (e.g. ``max_concurrent: 999``)
        is NOT malformed: it is a valid value the daemon clamps into range as a
        server-bound defense, and the block still applies fully.
        """
        if not isinstance(config, dict):
            return False
        generation = config.get("config_generation")
        # Validate BEFORE taking the lock or mutating anything, so a rejected
        # generation is a true no-op rather than a half-applied block whose
        # "rejected" verdict misdescribes the runner's real live state.
        unrecognized, malformed = self._classify_fields(config)
        fully_applicable = not unrecognized and not malformed

        with self._lock:
            if fully_applicable:
                mc = config.get("max_concurrent")
                if mc is not None:
                    self._max_concurrent = self._clamp(mc)
                mcc = config.get("max_concurrent_compute")
                if mcc is not None:
                    self._max_concurrent_compute = self._clamp(mcc)
                if "task_types" in config:
                    tt = config.get("task_types")
                    if tt == "all" or tt is None:
                        self._task_types = "all"
                    elif isinstance(tt, list):
                        self._task_types = [
                            str(x) for x in tt if isinstance(x, str) and x
                        ]
                if "gpu_indices_allowed" in config:
                    self._apply_gpu_allowlist_locked(config.get("gpu_indices_allowed"))
                if "gpus_per_job" in config:
                    self._apply_gpus_per_job_locked(config.get("gpus_per_job"))
                if isinstance(generation, str) and generation:
                    self._applied_generation = generation
                # A clean apply clears any prior rejection state (never let a
                # resolved reason linger after convergence).
                self._rejection_reason = None
                self._rejected_fields = None
                self._rejected_generation = None
            else:
                # Refused this generation. Mutate NOTHING. Record the offending
                # field name(s) + a stable human reason + the refused generation
                # so the operator sees WHICH field broke and that the runner's
                # live state is still the previous generation, not 'stuck'.
                sorted_unrecognized = sorted(unrecognized)
                sorted_malformed = sorted(malformed)
                reasons: list[str] = []
                if sorted_unrecognized:
                    reasons.append(
                        "unrecognized config field(s): "
                        + ", ".join(sorted_unrecognized)
                    )
                if sorted_malformed:
                    reasons.append(
                        "malformed config field(s): " + ", ".join(sorted_malformed)
                    )
                self._rejection_reason = "; ".join(reasons)
                self._rejected_fields = sorted(
                    set(sorted_unrecognized) | set(sorted_malformed)
                )
                self._rejected_generation = (
                    generation if isinstance(generation, str) and generation else None
                )
        return fully_applicable

    def _apply_gpu_allowlist_locked(self, raw) -> None:
        """Update the reported allowlist and the shared GPU-scheduling set.

        ``"all"``/``None`` → no restriction (every detected GPU); a list → that
        subset, intersected with detected GPUs so a NEW job is never assigned a
        GPU this host does not have. Running jobs keep their already-assigned
        GPUs (only the ``allowed`` set for FUTURE assignments changes).
        Called with ``self._lock`` held.
        """
        if raw is None or raw == "all":
            self._gpu_indices_allowed = "all"
            concrete = list(self._all_gpu_indices)
        elif isinstance(raw, list):
            wanted: list[int] = []
            seen: set[int] = set()
            for item in raw:
                if isinstance(item, bool):
                    continue
                if isinstance(item, int):
                    value = item
                elif isinstance(item, str) and item.strip().lstrip("+").isdecimal():
                    value = int(item.strip())
                else:
                    continue
                if value < 0 or value in seen:
                    continue
                seen.add(value)
                wanted.append(value)
            wanted.sort()
            self._gpu_indices_allowed = wanted
            concrete = (
                [i for i in wanted if i in self._all_gpu_indices]
                if self._all_gpu_indices
                else list(wanted)
            )
        else:
            return
        if self._gpu_state is not None:
            gpu_lock = self._gpu_state.get("lock")
            if gpu_lock is not None:
                with gpu_lock:
                    self._gpu_state["allowed"] = concrete
            else:
                self._gpu_state["allowed"] = concrete

    def _apply_gpus_per_job_locked(self, raw) -> None:
        """Update the reported per-job GPU count and the shared scheduling state.

        ``None`` keeps the current value (the server omitting the field is not a
        request to reset it). A valid value is clamped into
        ``[MIN_GPUS_PER_JOB, MAX_GPUS_PER_JOB]`` and written to
        ``gpu_state["per_job"]`` so the NEXT ``_assign_gpus`` reserves that many
        devices — running jobs keep the GPUs they were already assigned.
        Called with ``self._lock`` held.
        """
        if raw is None:
            return
        try:
            int(raw)
        except (TypeError, ValueError):
            return
        per_job = self._clamp_gpus_per_job(raw)
        self._gpus_per_job = per_job
        if self._gpu_state is not None:
            gpu_lock = self._gpu_state.get("lock")
            if gpu_lock is not None:
                with gpu_lock:
                    self._gpu_state["per_job"] = per_job
            else:
                self._gpu_state["per_job"] = per_job


# ── Heartbeat (background thread) ─────────────────────────────────────────────

_IMAGE_LLM_CLI_PROBE_CACHE: dict[
    tuple[str, str], tuple[float, dict[str, dict]]
] = {}
_IMAGE_LLM_CLI_PROBE_TTL_SECONDS = 300.0
_TRANSIENT_CLI_PROBE_REASONS = frozenset(
    {
        "version_probe_timeout",
        "version_probe_error",
        "version_probe_failed",
        "container_probe_error",
    }
)


def _agent_image_identity(agent_image: str) -> str | None:
    """Return the immutable ID of the locally selected Docker image.

    Tags are mutable, so keying CLI health only by ``repo:tag`` could keep a
    pre-deploy result after the same tag is rebuilt. An unresolved identity is
    deliberately not cacheable; the caller still probes the image and retries
    identity resolution on the next heartbeat.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format={{.Id}}",
                agent_image,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    image_id = (result.stdout or "").strip()
    return image_id if result.returncode == 0 and image_id else None


def _probe_llm_clis_for_agent_backend(
    agent_backend: str | None,
    agent_image: str | None,
) -> tuple[str, dict[str, dict]]:
    """Probe the CLI environment that the advertised agent backend will use."""
    if agent_backend == iso.BACKEND_DOCKER and agent_image:
        image_id = _agent_image_identity(agent_image)
        source = f"docker:{agent_image}@{image_id or 'unresolved'}"
        cache_key = (agent_image, image_id) if image_id else None
        now = time.monotonic()
        cached = (
            _IMAGE_LLM_CLI_PROBE_CACHE.get(cache_key)
            if cache_key is not None
            else None
        )
        if cached is not None and now - cached[0] < _IMAGE_LLM_CLI_PROBE_TTL_SECONDS:
            return source, {name: dict(info) for name, info in cached[1].items()}
        probed = iso.probe_container_llm_clis(agent_image)
        # Cache stable success/deterministic failure. A transport/timeout failure
        # is retried next heartbeat so the hysteresis below observes real probes,
        # not the same cached failure twice.
        if cache_key is not None and not any(
            isinstance(info, dict)
            and info.get("reason") in _TRANSIENT_CLI_PROBE_REASONS
            for info in probed.values()
        ):
            # Discard results for older IDs behind the same mutable tag.
            for old_key in tuple(_IMAGE_LLM_CLI_PROBE_CACHE):
                if old_key[0] == agent_image and old_key != cache_key:
                    _IMAGE_LLM_CLI_PROBE_CACHE.pop(old_key, None)
            _IMAGE_LLM_CLI_PROBE_CACHE[cache_key] = (
                now,
                {name: dict(info) for name, info in probed.items()},
            )
        return source, probed
    return "host", iso.probe_llm_clis(env=_cli_subprocess_env())


def _stabilize_llm_cli_probe(
    source: str,
    current: dict[str, dict],
    last_healthy: dict[tuple[str, str], dict],
    transient_failures: dict[tuple[str, str], int],
) -> dict[str, dict]:
    """Avoid one transient startup probe flapping otherwise healthy capacity.

    Deterministic failures (missing binary/native package, unsupported version)
    fail closed immediately. After a prior healthy observation, a timeout or
    generic non-zero probe gets one heartbeat of grace and must fail again before
    eligibility is withdrawn. Cold-start failures never receive grace.
    """
    stabilized: dict[str, dict] = {}
    for name, raw in current.items():
        info = dict(raw) if isinstance(raw, dict) else {}
        key = (source, name)
        if info.get("healthy") is True:
            last_healthy[key] = dict(info)
            transient_failures.pop(key, None)
            stabilized[name] = info
            continue
        if info.get("reason") in _TRANSIENT_CLI_PROBE_REASONS and key in last_healthy:
            count = transient_failures.get(key, 0) + 1
            transient_failures[key] = count
            if count < 2:
                previous = dict(last_healthy[key])
                previous["probe_warning"] = info.get("reason")
                previous["probe_stale"] = True
                stabilized[name] = previous
                continue
        else:
            transient_failures.pop(key, None)
        stabilized[name] = info
    return stabilized


def _llm_cli_is_eligible(info: dict) -> bool:
    healthy = info.get("healthy")
    return bool(healthy) if isinstance(healthy, bool) else bool(info.get("present"))


def _intersect_llm_cli_health(
    inventories: dict[str, dict[str, dict]],
) -> dict[str, dict]:
    """Conservatively flatten provider health across selectable agent backends.

    The heartbeat schema is provider-flat, while a hybrid runner routes light
    read-only work through Docker and write/heavy work through host bwrap. Until
    the schema can express per-task-slice health, a provider is dispatchable only
    when it is healthy on every selectable backend. Other providers are evaluated
    independently, so one broken Codex installation does not suppress Claude,
    Gemini, or compute capacity.
    """
    names = {
        name
        for inventory in inventories.values()
        for name in inventory
    }
    combined: dict[str, dict] = {}
    for name in sorted(names):
        details = {
            backend: (
                dict(inventory[name])
                if isinstance(inventory.get(name), dict)
                else {
                    "present": False,
                    "healthy": False,
                    "version": "",
                    "reason": "not_reported",
                }
            )
            for backend, inventory in inventories.items()
        }
        backend_health = {
            backend: _llm_cli_is_eligible(info)
            for backend, info in details.items()
        }
        failed = [
            backend for backend, healthy in backend_health.items() if not healthy
        ]
        selected = (
            next(info for backend, info in details.items() if backend in failed)
            if failed
            else next(iter(details.values()))
        )
        selected["backend_health"] = backend_health
        selected["backend_versions"] = {
            backend: info.get("version") or None
            for backend, info in details.items()
        }
        if failed:
            selected.update(
                {
                    "present": False,
                    "healthy": False,
                    "reason": "unhealthy_on_selectable_backend:"
                    + ",".join(sorted(failed)),
                }
            )
        else:
            selected["present"] = True
            selected["healthy"] = True
            selected["reason"] = None
        combined[name] = selected
    return combined

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
    live_config: "_LiveRunnerConfig | None" = None,
    project_verification: dict | None = None,
    resource_ledger: "_ResourceLedger | None" = None,
    gpu_state: dict | None = None,
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
    deferred_auto_update_hint: str | None = None
    # Whether ANY heartbeat has ever authenticated (200/204). A token that has
    # never authenticated and gets a 401 is definitively bad (wrong/revoked, or a
    # deleted runner) — the daemon fast-fails on that first 401 rather than
    # retrying ~max_auth_failures times as if it were transient. A token that DID
    # authenticate before keeps the existing re-arm/streak behavior (an inactive
    # row re-arms on an authenticated heartbeat; a mid-life revoke is retried).
    ever_authenticated = False
    _llm_cli_last_healthy: dict[tuple[str, str], dict] = {}
    _llm_cli_transient_failures: dict[tuple[str, str], int] = {}
    # Startup detection already made the first proof. Delay a retry until the
    # bounded cadence elapses, then refresh only recoverable failures so a cold
    # image pull or temporary registry/runtime outage does not require restart.
    _gpu_passthrough_last_retry_at = time.monotonic()
    while not _shutdown_event.is_set():
        try:
            caps, _gpu_passthrough_last_retry_at = _retry_transient_gpu_passthrough(
                caps, _gpu_passthrough_last_retry_at
            )
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
            # Probe the node runtime once and reuse it below — this is the single
            # source of truth for both the bwrap advertisement leg and the per-leg
            # breakdown, so the compound reason string cannot disagree with the
            # advertisement (an earlier version re-probed inside the reason chain).
            node_runtime_ok = _node_runtime_ok()
            bwrap_agent_ok = (
                agent_sandbox_ok
                and agent_binary_present
                and node_runtime_ok
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
            # Per-leg breakdown of the FOUR independent legs that gate the agent
            # backends, so the fleet UI can render a checklist rather than parse a
            # string. Emitted whenever the leg states are known (caps is not None);
            # None for the old-caller path below, matching the absent-tolerant
            # heartbeat contract. Note the docker leg does NOT require the host
            # node/binary (the runtime lives in the prod image), while the bwrap
            # path requires sandbox + dist + node together.
            if caps is not None:
                agent_unavailable_legs: dict | None = {
                    "bwrap_sandbox": bool(agent_sandbox_ok),
                    "agent_dist": bool(agent_binary_present),
                    "node_runtime": bool(node_runtime_ok),
                    "docker": bool(docker_agent_ok),
                }
            else:
                agent_unavailable_legs = None
            # Granular unavailability reason (engine's FleetRunnersPanel
            # observability field): when NEITHER backend leg is satisfiable, say
            # WHY so the orchestrator/operator sees the cause. Enumerate EVERY
            # failing leg (compound, "; "-joined) rather than stopping at the first
            # match, so the operator can tell precisely which legs failed instead of
            # an opaque single-cause string. None when agent_available is True.
            if agent_available:
                agent_unavailable_reason: str | None = None
            else:
                # Ordered (failed-flag, phrase) pairs — a leg contributes its phrase
                # only when it actually failed. The bwrap trio (sandbox/dist/node)
                # and the docker leg are all independent, so a sandbox+no-docker box
                # now surfaces BOTH causes instead of folding them into one string.
                _leg_reasons = [
                    (not agent_sandbox_ok, "bwrap sandbox unavailable"),
                    (not agent_binary_present, "agent dist not built"),
                    (not node_runtime_ok, "node runtime not functional"),
                    (not docker_agent_ok, "docker agent unavailable"),
                ]
                failed_phrases = [phrase for failed, phrase in _leg_reasons if failed]
                agent_unavailable_reason = (
                    "; ".join(failed_phrases)
                    if failed_phrases
                    else "no usable agent backend (bwrap or docker)"
                )
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
                # A hybrid runner has two selectable task slices: Docker for
                # light read-only tasks and host bwrap for write/heavy tasks.
                # Probe/stabilize each leg independently, then conservatively
                # intersect each provider into the flat heartbeat schema.
                probe_targets: list[tuple[str, str]] = []
                if bwrap_agent_ok:
                    probe_targets.append(("host", iso.BACKEND_BWRAP))
                if docker_agent_ok:
                    probe_targets.append(("docker", iso.BACKEND_DOCKER))
                if not probe_targets:
                    probe_targets.append(("host", agent_backend))
                backend_inventories: dict[str, dict[str, dict]] = {}
                for label, backend in probe_targets:
                    _cli_source, probed_clis = _probe_llm_clis_for_agent_backend(
                        backend, agent_image
                    )
                    backend_inventories[label] = _stabilize_llm_cli_probe(
                        _cli_source,
                        probed_clis,
                        _llm_cli_last_healthy,
                        _llm_cli_transient_failures,
                    )
                llm_clis = (
                    _intersect_llm_cli_health(backend_inventories)
                    if len(backend_inventories) > 1
                    else next(iter(backend_inventories.values()))
                )
                # New explicit health is authoritative. Normalize it into the
                # legacy dispatch bit as well, so an older orchestrator that
                # only understands ``present`` still fails closed.
                llm_clis = {
                    name: (
                        {**info, "present": False}
                        if isinstance(info, dict) and info.get("healthy") is False
                        else info
                    )
                    for name, info in llm_clis.items()
                }
            else:
                isolation_backends = None
                agent_backend = None
                compute_backend = None
                llm_clis = None
            # Flat CLI inventory derived from the same probe: {name: version|None}.
            # New probes report an explicit ``healthy`` bit; it takes precedence
            # over the legacy ``present`` bit so a PATH shim whose native package
            # failed to load is never advertised as usable. Older two-field probe
            # dictionaries remain compatible through the ``present`` fallback.
            cli_inventory = (
                {
                    name: (
                        (info.get("version") or "")
                        if _llm_cli_is_eligible(info)
                        else None
                    )
                    for name, info in llm_clis.items()
                    if isinstance(info, dict)
                }
                if isinstance(llm_clis, dict)
                else None
            )
            gpu_memory = _gpu_memory_mib() if has_gpu else None
            # Container layers may live on a different filesystem than compute
            # scratch. Report both so dispatch can gate image pulls on the real
            # limiting capacity while bwrap work remains workspace-only.
            workspace_free_bytes = _disk_free_bytes(workspace_root)
            container_image_store = _container_image_store(caps)
            # Per-project docker image-cache accounting. Only probed when docker
            # (or podman) is the selected compute backend — a bwrap-only / GCP
            # runner has no docker image cache to account, and we skip the two
            # subprocess calls entirely. Best-effort: None ⇒ field omitted.
            image_cache = (
                _docker_image_cache_by_project()
                if compute_backend in (iso.BACKEND_DOCKER, iso.BACKEND_PODMAN)
                else None
            )
            body = {
                "active_task_ids": task_ids,
                "active_compute_job_ids": compute_job_ids,
                # This is a genuine active-work report: task_ids/compute_job_ids
                # are the live running-agents set. The orchestrator refreshes its
                # reaper-trusted active-set snapshot + stamp only for reports
                # flagged here (a --check preflight sets this False).
                "active_work_report": True,
                # Version mismatch drains both claim loops while existing work
                # finishes. Tell the server this heartbeat is not availability.
                "update_draining": _update_in_progress.is_set(),
                "agent_available": agent_available,
                "agent_unavailable_reason": agent_unavailable_reason,
                "agent_unavailable_legs": agent_unavailable_legs,
                "supported_agent_task_types": supported_agent_task_types,
                "hostname": hostname,
                "version": version,
                "isolation_backends": isolation_backends,
                "container_gpu_passthrough": (
                    caps.container_gpu_passthrough if caps is not None else None
                ),
                "agent_backend": agent_backend,
                "compute_backend": compute_backend,
                "llm_clis": llm_clis,
                "cli_inventory": cli_inventory,
                # Health telemetry consumed by the orchestrator to spot a runner
                # that is failing every job / running low on disk / low on memory /
                # pegged CPU.
                "last_successful_job_at": last_success_at,
                "consecutive_job_failures": consecutive_failures,
                # Legacy field remains the workspace filesystem for existing
                # low-disk health telemetry and imageless/bwrap behavior.
                "disk_free_bytes": workspace_free_bytes,
                "workspace_free_bytes": workspace_free_bytes,
                "container_image_store_runtime": (
                    container_image_store.get("runtime") if container_image_store else None
                ),
                "container_image_store_path": (
                    container_image_store.get("root") if container_image_store else None
                ),
                "container_image_store_free_bytes": (
                    container_image_store.get("free_bytes") if container_image_store else None
                ),
                "container_image_reclaim": (
                    telemetry.container_image_reclaim_snapshot() if telemetry else None
                ),
                # Per-project docker image-cache accounting (multi-project
                # runners share one image cache; this attributes its size back
                # to the project that pulled each image). Omitted when docker is
                # not the compute backend.
                "image_cache_by_project": image_cache,
                "cpu_load_avg": _cpu_load_avg(),
                "memory_free_bytes": _memory_free_bytes(),
                # New, additive field. Older orchestrators ignore it; newer fleet
                # surfaces can use it to explain why a high-concurrency runner is
                # temporarily declining work rather than looking mysteriously idle.
                "resource_capacity": (
                    resource_ledger.snapshot(gpu_state)
                    if resource_ledger is not None else None
                ),
                "gpu_utilization_pct": _gpu_utilization_pct() if has_gpu else None,
                # Free-VRAM list (read by dispatch VRAM-fit gating) derived from
                # the superset (total, free) probe — no second nvidia-smi call.
                "gpu_free_vram_mib": gpu_memory[1] if gpu_memory else None,
                **gpu_capabilities,
            }
            if project_verification is not None:
                body["project_verification"] = project_verification
            # Per-GPU VRAM (probe failure ⇒ fields omitted entirely, matching
            # the schema's absent-tolerant contract for old runners).
            if gpu_memory is not None:
                body["gpu_memory_total_mib"], body["gpu_memory_free_mib"] = gpu_memory
            # Applied-vs-pending config report: echo the live-safe generation +
            # values the daemon currently has in effect so the orchestrator/UI
            # can tell desired-vs-applied apart. Reflects what was applied on the
            # PREVIOUS tick (the config block from THIS response is applied below
            # and reported on the FOLLOWING heartbeat).
            if live_config is not None:
                body.update(live_config.report())
            status, resp_body = _post(url, token, body)
            if status in (200, 204):
                consecutive_401 = 0
                consecutive_network_failures = 0
                ever_authenticated = True
                # Parse orchestrator version hint so the runner can detect staleness.
                if isinstance(resp_body, dict):
                    # Apply the server-authoritative live-safe config block (if
                    # present) onto the running process. Live-safe fields take
                    # effect on the next claim tick without disrupting in-flight
                    # work; the applied generation is echoed on the next
                    # heartbeat (see body.update above).
                    if live_config is not None:
                        live_config.apply(resp_body.get("config"))
                    version_hint = resp_body.get("version_hint")
                    if version_hint and version:
                        runner_sha = _describe_sha(version)
                        if runner_sha:
                            stale = not version_hint.startswith(runner_sha)
                        else:
                            stale = version_hint not in version
                        if stale:
                            # Enter drain mode immediately. Both agent and
                            # compute pollers stop NEW claims while existing
                            # work finishes, guaranteeing a long-lived resident
                            # cannot starve a runner rollout forever.
                            _update_in_progress.set()
                            # Check if runner is completely idle before attempting
                            # auto-update (no active tasks and no compute jobs).
                            with lock:
                                has_tasks = bool(active_tasks)
                            if active_compute_jobs is not None and compute_lock is not None:
                                with compute_lock:
                                    has_compute = bool(active_compute_jobs)
                            else:
                                has_compute = False

                            if deferred_auto_update_hint == version_hint:
                                # A prior update proved this exact server hint is
                                # behind/different from origin/main. Do not
                                # re-arm drain mode forever on every heartbeat;
                                # wait for the server hint to change.
                                _update_in_progress.clear()
                            elif not has_tasks and not has_compute:
                                print(
                                    f"[runner] version mismatch ({version!r} vs "
                                    f"{version_hint!r}) and runner is idle — "
                                    "auto-updating...",
                                    flush=True,
                                )
                                try:
                                    result = br.do_update()
                                except Exception:
                                    _update_in_progress.clear()
                                    raise
                                if result == 0:
                                    refreshed_version = _runner_version()
                                    refreshed_sha = _describe_sha(refreshed_version)
                                    refreshed_matches = (
                                        bool(refreshed_sha)
                                        and version_hint.startswith(refreshed_sha)
                                    ) or (
                                        bool(refreshed_version)
                                        and version_hint in refreshed_version
                                    )
                                    if refreshed_matches:
                                        # Update succeeded and the checkout now
                                        # matches the server. _restart_runner_service
                                        # (called inside do_update) may already have
                                        # killed this process; otherwise re-exec here.
                                        _reexec_after_update()
                                    else:
                                        # ``do_update`` returns 0 for a no-op when
                                        # origin/main is already newer than the
                                        # deployed orchestrator. Exiting here used
                                        # to make a systemd runner hit its start
                                        # limit even though it was healthy.
                                        deferred_auto_update_hint = version_hint
                                        _update_in_progress.clear()
                                        print(
                                            "[runner] update completed but the runner "
                                            "still differs from the orchestrator; "
                                            "continuing without restart until its "
                                            "version hint changes.",
                                            flush=True,
                                        )
                                else:
                                    print(
                                        f"[runner] auto-update failed "
                                        f"(exit={result}) — continuing with "
                                        "current version.",
                                        flush=True,
                                    )
                                    _update_in_progress.clear()
                            elif has_tasks or has_compute:
                                print(
                                    f"[runner] WARNING: runner version {version!r} "
                                    f"does not match orchestrator version hint "
                                    f"{version_hint!r} — draining new claims; "
                                    "auto-update will start when current work finishes.",
                                    flush=True,
                                )
                        else:
                            deferred_auto_update_hint = None
                            _update_in_progress.clear()
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
                if not ever_authenticated:
                    # First-ever contact was rejected AND the token on disk did not
                    # change (the reload branch above would have `continue`d). This
                    # token has never authenticated, so it is almost certainly wrong,
                    # revoked, or for a deleted runner — retrying it max_auth_failures
                    # times just delays an inevitable failure behind an optimistic
                    # "waiting…" spinner. Fail fast with an actionable message.
                    _die(
                        "heartbeat rejected with 401 and this runner token has never "
                        "authenticated — it is almost certainly wrong, revoked, or for "
                        "a deleted runner. Re-copy the token from the Blueprint console "
                        "(Fleet -> Enroll runner) and restart, or write the correct "
                        "token to the runner's token file. Exiting non-zero instead of "
                        "retrying a token that has never worked."
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
    """Run the available container runtime's login using credentials in *auth*.

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

    runtime = (
        "docker" if shutil.which("docker") else "podman" if shutil.which("podman") else None
    )
    if runtime is None:
        print("[compute] container runtime login unavailable (docker/podman not installed)", flush=True)
        return False

    cmd = [runtime, "login", "--password-stdin"]
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
_GPU_PASSTHROUGH_RETRY_SECONDS = 300.0


def _get_capabilities() -> "iso.Capabilities":
    """Detect (and cache) host isolation capabilities once for the process."""
    global _CAPS_CACHE
    if _CAPS_CACHE is None:
        _CAPS_CACHE = iso.detect_capabilities()
    return _CAPS_CACHE


def _retry_transient_gpu_passthrough(
    caps: "iso.Capabilities | None", last_retry_at: float, *, now: float | None = None
) -> tuple["iso.Capabilities | None", float]:
    """Refresh a recoverable container GPU proof at a bounded heartbeat cadence."""
    if caps is None or not iso.container_gpu_passthrough_needs_retry(caps):
        return caps, last_retry_at
    current = time.monotonic() if now is None else now
    if current - last_retry_at < _GPU_PASSTHROUGH_RETRY_SECONDS:
        return caps, last_retry_at
    return iso.refresh_container_gpu_passthrough(caps), current


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
        # Defense-in-depth for the unified multi-project ledger. One daemon per
        # box serves jobs from every allow-listed project through this single
        # ``in_use`` set precisely so two jobs — from ANY projects — can never
        # hold the same physical GPU (the double-booking bug two per-project
        # daemons created). ``assign_gpu_indices`` already excludes in-use
        # indices, but a corrupted ledger or a future refactor must never
        # SILENTLY double-book a device: refuse the assignment (job runs without
        # a fresh GPU) rather than hand out an index another project already
        # holds.
        overlap = set(assigned) & gpu_state["in_use"]
        if overlap:
            print(
                f"[compute] GPU ledger inconsistency: index(es) {sorted(overlap)} "
                "already in use; refusing to double-book — job runs without a "
                "fresh device",
                flush=True,
            )
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
    progress: dict | None = None,
    resume_hydration: dict | None = None,
    artifact_delivery: dict | None = None,
    viewable_omissions: dict | None = None,
) -> bool:
    """POST a status report for a compute job. Returns cancel_requested.

    ``progress`` is the optional bounded interim-progress tail from
    :class:`_ProgressTail` (``{"records": [...], "count": n}``); absent when the
    job never wrote a progress record. An old backend simply ignores the field.

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
    if progress is not None:
        body["progress"] = progress
    if resume_hydration is not None:
        body["resume_hydration"] = resume_hydration
    if artifact_delivery is not None:
        body["artifact_delivery"] = artifact_delivery
    if viewable_omissions is not None:
        body["viewable_omissions"] = viewable_omissions

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


class _ComputeJobReportHeartbeat:
    """Keep a claimed compute job live while runner-side I/O can take minutes.

    Artifact archiving/upload and checkpoint hydration happen outside the command
    polling loop, so they do not otherwise emit compute-job reports.  The backend
    uses those reports as the liveness lease for runner-subprocess jobs.
    """

    def __init__(self, orchestrator_url: str, token: str, job_id: str):
        self._orchestrator_url = orchestrator_url
        self._token = token
        self._job_id = job_id
        self._stop = threading.Event()
        self._cancel_requested = threading.Event()
        self._thread: threading.Thread | None = None

    def _send(self) -> None:
        try:
            if _report_compute_job(
                self._orchestrator_url, self._token, self._job_id,
                exec_status="running",
            ):
                self._cancel_requested.set()
        except Exception:  # noqa: BLE001 -- liveness is best-effort
            pass

    @property
    def cancel_requested(self) -> bool:
        """Whether any liveness report observed an orchestrator cancellation."""
        return self._cancel_requested.is_set()

    def _run(self) -> None:
        while not self._stop.wait(_RUNNER_HEARTBEAT_INTERVAL):
            self._send()

    def __enter__(self):
        # Send before starting expensive I/O: a resumed multi-GB download must
        # not wait for its first heartbeat interval before acquiring its lease.
        self._send()
        self._thread = threading.Thread(
            target=self._run, name=f"compute-report-{self._job_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            # Do not delay terminal reporting behind a stalled best-effort report.
            self._thread.join(timeout=1)


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


def _classify_viewable(filename: str) -> "tuple[str, str] | None":
    """Return ``(content_type, kind)`` for a viewable filename, else ``None``.

    Mirrors ``app.storage.viewable_content_type_for_filename``: classification is
    by the lowercased extension only (png/jpg/jpeg/webp/gif/svg → image, mp4/webm →
    video). A name with no recognised viewable extension yields ``None``.
    """
    if not isinstance(filename, str) or "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[-1].lower()
    return _VIEWABLE_CONTENT_TYPES.get(ext)


def _scan_viewable_files(artifact_dir: str) -> list:
    """Scan *artifact_dir* for viewable files; return sorted candidate dicts.

    Each candidate is ``{"relpath", "abspath", "size", "mtime", "kind",
    "content_type"}``. ``relpath`` is POSIX-joined and relative to
    *artifact_dir* so arm-subdir context survives (matching the server's
    ``sanitize_viewable_relpath``). Mirrors the HOST-side scan policy in
    ``app.compute_backends._collect_viewable_candidates_from_dir``:

      * extension allowlist only (png/jpg/jpeg/webp/gif/mp4/webm/svg/pdf);
      * symlinked files skipped and symlinked dirs never descended
        (``os.walk(followlinks=False)`` + ``os.path.islink``) — a planted symlink
        must never exfiltrate an operator secret via a viewer-accessible object;
      * non-regular files (fifos/devices) and zero-byte files skipped;
      * results sorted by relpath.

    Per-kind byte caps and the 25-file budget are NOT applied here (the uploader
    owns those, since the budget is tracked across heartbeat cycles). Best-effort
    and total: an ``OSError`` on a single entry is swallowed so a transient stat
    failure never aborts the whole scan. A missing or symlinked artifact dir
    yields ``[]``.
    """
    if (
        not artifact_dir
        or not os.path.isdir(artifact_dir)
        or os.path.islink(artifact_dir)
    ):
        return []
    candidates: list = []
    scanned = 0
    for root, dirs, names in os.walk(artifact_dir, followlinks=False):
        # Defense in depth: prune symlinked dirs so a swapped entry cannot leak
        # even if the platform's walk were to follow them.
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in names:
            if scanned >= _VIEWABLE_SCAN_FILE_CAP:
                # Return what we have rather than walking an unbounded directory.
                candidates.sort(key=lambda c: c["relpath"])
                return candidates
            scanned += 1
            abspath = os.path.join(root, name)
            try:
                if os.path.islink(abspath):
                    continue
                classified = _classify_viewable(name)
                if classified is None:
                    continue
                content_type, kind = classified
                st = os.stat(abspath)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size <= 0:
                    continue
                relpath = os.path.relpath(abspath, artifact_dir).replace(os.sep, "/")
                candidates.append(
                    {
                        "relpath": relpath,
                        "abspath": abspath,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "mtime_ns": st.st_mtime_ns,
                        "ctime_ns": st.st_ctime_ns,
                        "kind": kind,
                        "content_type": content_type,
                    }
                )
            except OSError:
                continue
    candidates.sort(key=lambda c: c["relpath"])
    return candidates


_VIEWABLE_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _multipart_viewable_chunks(
    path: str,
    expected_size: int,
    cap: int,
    boundary: str,
    relpath: str,
    content_type: str,
):
    """Yield one multipart viewable body without ever buffering its file part.

    The descriptor is opened with ``O_NOFOLLOW`` in the uploader thread.  Its
    size is revalidated before constructing the request so a file changed after
    the scan is retried on the next heartbeat rather than sending a stale or
    over-cap body.  ``remaining`` also prevents a concurrent append from
    exceeding the advertised Content-Length/cap mid-transfer.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        with os.fdopen(fd, "rb", closefd=True) as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size != expected_size or not (
                0 < expected_size <= cap
            ):
                return
            bnd = boundary.encode("latin-1")
            crlf = b"\r\n"
            yield b"--" + bnd + crlf
            yield b'Content-Disposition: form-data; name="filename"' + crlf + crlf
            yield relpath.encode() + crlf
            yield b"--" + bnd + crlf
            yield (
                f'Content-Disposition: form-data; name="file"; filename="{relpath}"'.encode()
                + crlf
            )
            yield f"Content-Type: {content_type}".encode() + crlf + crlf
            remaining = expected_size
            while remaining:
                chunk = fh.read(min(_VIEWABLE_UPLOAD_CHUNK_BYTES, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk
    except OSError:
        return
    yield crlf + b"--" + bnd + b"--" + crlf


def _multipart_viewable_chunks_from_file(
    fh, expected_size: int, boundary: str, relpath: str, content_type: str
):
    """Yield a multipart body from an already-open, size-checked descriptor."""
    bnd = boundary.encode("latin-1")
    crlf = b"\r\n"
    yield b"--" + bnd + crlf
    yield b'Content-Disposition: form-data; name="filename"' + crlf + crlf
    yield relpath.encode() + crlf
    yield b"--" + bnd + crlf
    yield (
        f'Content-Disposition: form-data; name="file"; filename="{relpath}"'.encode()
        + crlf
    )
    yield f"Content-Type: {content_type}".encode() + crlf + crlf
    remaining = expected_size
    while remaining:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size != expected_size:
            raise OSError("viewable artifact changed during upload")
        chunk = fh.read(min(_VIEWABLE_UPLOAD_CHUNK_BYTES, remaining))
        if not chunk:
            raise OSError("viewable artifact truncated during upload")
        remaining -= len(chunk)
        yield chunk
    st = os.fstat(fh.fileno())
    if not stat.S_ISREG(st.st_mode) or st.st_size != expected_size:
        raise OSError("viewable artifact changed during upload")
    yield crlf + b"--" + bnd + b"--" + crlf


def _viewable_multipart_length(boundary: str, relpath: str, size: int, content_type: str) -> int:
    """Return the exact content length emitted by ``_multipart_viewable_chunks``."""
    bnd = boundary.encode("latin-1")
    crlf = b"\r\n"
    headers = (
        b"--" + bnd + crlf,
        b'Content-Disposition: form-data; name="filename"' + crlf + crlf,
        relpath.encode() + crlf,
        b"--" + bnd + crlf,
        f'Content-Disposition: form-data; name="file"; filename="{relpath}"'.encode()
        + crlf,
        f"Content-Type: {content_type}".encode() + crlf + crlf,
        crlf + b"--" + bnd + b"--" + crlf,
    )
    return sum(map(len, headers)) + size


class _ViewableStreamState:
    """Cross-heartbeat state for mid-run viewable streaming of ONE compute job.

    ``sent`` maps an uploaded ``relpath`` to its last-uploaded nanosecond stat
    signature so an unchanged file is not re-sent every cycle while an overwritten
    one (e.g. ``reward_curve.png`` rewritten each eval step) IS re-sent. The
    ``*_logged`` members keep the bounded skip notices to one line each.

    ``scan_start`` tracks the relpath after which the next scan begins to ensure
    fairness: a frequently-rewritten early-sorted file cannot monopolize every
    heartbeat slot and starve later files waiting for their first upload. The
    scan rotates to start just past the last-uploaded relpath so every eligible
    candidate sees a slot within at most N heartbeats (N = distinct paths).
    """

    __slots__ = (
        "sent",
        "revisions",
        "oversize_logged",
        "budget_logged",
        "oversize_relpaths",
        "count_cap_relpaths",
        "scan_start",
        "inflight",
        "completed",
        "lock",
        "thread",
    )

    def __init__(self) -> None:
        self.sent: dict = {}
        # relpath -> (stat signature, opaque generation token). A failed upload
        # retries with the SAME token; a changed stat signature gets a new one.
        self.revisions: dict = {}
        self.oversize_logged: set = set()
        self.budget_logged = False
        # Deduplicated sets of relpaths skipped due to per-kind size cap or the
        # 25-file count cap. Set membership prevents repeated scans from inflating
        # counts. Reported to the backend on each running heartbeat so the operator
        # can see live omissions before the job reaches a terminal state.
        self.oversize_relpaths: set = set()
        self.count_cap_relpaths: set = set()
        # Round-robin fairness cursor: the relpath of the last successfully
        # started upload. Next scan begins just past this path so a file whose
        # relpath sorts first cannot monopolize every heartbeat slot.
        self.scan_start: str = ""
        self.inflight: tuple | None = None
        self.completed: tuple | None = None
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def revision_for(self, relpath: str, signature: tuple) -> str:
        """Return a stable token for this observed local-file generation."""
        current = self.revisions.get(relpath)
        if current is None or current[0] != signature:
            current = (signature, secrets.token_hex(16))
            self.revisions[relpath] = current
        return current[1]

    def reap(self) -> None:
        """Commit only completed successful uploads on the heartbeat thread."""
        with self.lock:
            completed, self.completed = self.completed, None
        if completed is None:
            return
        relpath, signature, status = completed
        if status is not None and 200 <= status < 300:
            self.sent[relpath] = signature

    def start_upload(self, upload_args: tuple, relpath: str, signature: tuple) -> bool:
        """Start at most one daemon delivery; never block the heartbeat loop."""
        with self.lock:
            if self.inflight is not None:
                return False
            self.inflight = (relpath, signature)

        def _deliver() -> None:
            try:
                status = _upload_viewable(*upload_args)
            except Exception as exc:  # pragma: no cover - defensive worker boundary
                print(f"[compute] unexpected viewable uploader error: {exc}", flush=True)
                status = None
            with self.lock:
                self.completed = (relpath, signature, status)
                self.inflight = None

        self.thread = threading.Thread(target=_deliver, daemon=True)
        self.thread.start()
        return True

    def drain_upload(self, timeout: "float | None" = None) -> bool:
        """Wait up to ``timeout`` for one delivery and commit it when complete.

        Returns ``False`` while the daemon upload is still running.  Terminal
        handoff uses that signal to abandon the remaining best-effort previews
        instead of holding a compute runner until the transport timeout.
        """
        with self.lock:
            thread = self.thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
        self.reap()
        return True


def _upload_viewable(
    orchestrator_url: str,
    token: str,
    job_id: str,
    relpath: str,
    path: str,
    size: int,
    content_type: str,
    revision: "str | None" = None,
) -> "int | None":
    """POST one viewable file to the mid-run endpoint. Returns HTTP status, or
    ``None`` on a network error. Never raises — mid-run streaming is strictly
    best-effort and must never stall or fail the compute job."""
    cap = (
        _MAX_VIEWABLE_VIDEO_BYTES
        if content_type.startswith("video/")
        else _MAX_VIEWABLE_IMAGE_BYTES
    )
    if not 0 < size <= cap:
        return None
    boundary = "----blueprintrunner" + os.urandom(16).hex()
    ctype = f"multipart/form-data; boundary={boundary}"
    url = f"{orchestrator_url}/api/runner/compute-job/{job_id}/viewable"
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb", closefd=True) as fh:
            st = os.fstat(fh.fileno())
            # Validate before Request/urlopen; stale scan results retry next heartbeat.
            if not stat.S_ISREG(st.st_mode) or st.st_size != size:
                return None
            req = urllib.request.Request(
                url,
                data=_multipart_viewable_chunks_from_file(
                    fh, size, boundary, relpath, content_type
                ),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": ctype,
                    "Accept": "application/json",
                    "Content-Length": str(
                        _viewable_multipart_length(boundary, relpath, size, content_type)
                    ),
                    **(
                        {"X-Blueprint-Viewable-Revision": revision}
                        if revision
                        else {}
                    ),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return int(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001 — network blip/timeout is non-fatal
        print(
            f"[compute] viewable upload network error for job {job_id} "
            f"({relpath}): {exc}",
            flush=True,
        )
        return None


def _stream_viewable_files(
    orchestrator_url: str,
    token: str,
    job_id: str,
    artifact_dir: str,
    state: "_ViewableStreamState",
    *,
    drain: bool = False,
) -> None:
    """Best-effort mid-run push of NEW-or-CHANGED viewable files. NEVER raises.

    Invoked on the heartbeat cadence from ``_run_compute_job``. Uploads each
    viewable file whose nanosecond stat signature changed since its last upload
    (so an overwritten ``reward_curve.png`` re-uploads to refresh the live preview
    but a stable file is not re-sent every cycle), enforcing the same per-kind
    size cap and 25-distinct-file budget the server enforces — oversize/over-budget
    files are SKIPPED with a bounded one-line notice, never crashing. Every upload
    failure (network blip, 4xx, 5xx) is swallowed and streaming continues, exactly
    like the interim ``running`` progress reports.
    """
    # Reset per-scan skip counters so the omissions reported to the backend
    # reflect THIS heartbeat's scan, not files skipped in earlier cycles that
    # may have since been deleted or resized.  The *_logged guards remain
    # persistent so each file emits at most one log line over the job lifetime.
    state.oversize_relpaths = set()
    state.count_cap_relpaths = set()

    try:
        candidates = _scan_viewable_files(artifact_dir)
    except Exception as exc:  # noqa: BLE001 — scanning is strictly best-effort
        print(f"[compute] viewable scan error for job {job_id}: {exc}", flush=True)
        return

    drain_deadline = (
        time.monotonic() + _FINAL_VIEWABLE_DRAIN_SECONDS if drain else None
    )

    def _drain_within_deadline() -> bool:
        if drain_deadline is None:
            return True
        remaining = max(0.0, drain_deadline - time.monotonic())
        if state.drain_upload(timeout=remaining):
            return True
        print(
            f"[compute] final viewable drain exceeded "
            f"{_FINAL_VIEWABLE_DRAIN_SECONDS:g}s for job {job_id}; "
            "continuing terminal handoff",
            flush=True,
        )
        return False

    if drain:
        if not _drain_within_deadline():
            return
    else:
        state.reap()

    # Fairness: rotate the candidate list so scanning starts just past the
    # relpath of the last uploaded file. Candidates are sorted by relpath, so
    # rotating ensures each distinct path sees a slot within at most N
    # heartbeats (N = number of distinct viewable paths). A frequently-rewritten
    # early-sorted file cannot starve later stable files waiting for upload.
    if state.scan_start and candidates:
        start_idx = next(
            (i for i, c in enumerate(candidates) if c["relpath"] > state.scan_start),
            0,  # scan_start was the last path or beyond — wrap to beginning
        )
        if start_idx:
            candidates = candidates[start_idx:] + candidates[:start_idx]

    for cand in candidates:
        relpath = cand["relpath"]
        kind = cand["kind"]
        size = cand["size"]
        cap = _MAX_VIEWABLE_VIDEO_BYTES if kind == "video" else _MAX_VIEWABLE_IMAGE_BYTES

        # Per-kind size cap: the server would 413 it, so skip locally (log once).
        if size > cap:
            if relpath not in state.oversize_logged:
                state.oversize_logged.add(relpath)
                print(
                    f"[compute] viewable {relpath} ({size} bytes) exceeds the "
                    f"{cap}-byte {kind} cap for job {job_id}; skipping live preview",
                    flush=True,
                )
            state.oversize_relpaths.add(relpath)
            continue

        # Nanosecond mtime + ctime detects same-size overwrites while remaining
        # cheap enough for heartbeat scans (hashing a 512 MiB rollout every poll
        # would violate the bounded preview path). The generated token below is
        # stable across retries for this exact observed generation.
        signature = (cand["mtime_ns"], cand["ctime_ns"], size)
        if state.sent.get(relpath) == signature:
            continue  # unchanged since last upload — don't re-send

        # 25-distinct-file budget: a NEW relpath beyond the cap is skipped, but an
        # already-sent relpath (overwritten file) still re-uploads to refresh.
        if relpath not in state.sent and len(state.sent) >= _MAX_VIEWABLE_FILE_COUNT:
            if not state.budget_logged:
                state.budget_logged = True
                print(
                    f"[compute] viewable budget ({_MAX_VIEWABLE_FILE_COUNT}) reached "
                    f"for job {job_id}; further new live previews skipped",
                    flush=True,
                )
            state.count_cap_relpaths.add(relpath)
            continue

        # Slow preview uploads run outside the report/cancellation loop.  A single
        # in-flight slot bounds memory and background I/O; subsequent candidates
        # are considered next heartbeat.  ``sent`` is updated by ``reap`` only
        # after the worker receives a definitive 2xx response.
        if not state.start_upload(
            (
                orchestrator_url,
                token,
                job_id,
                relpath,
                cand["abspath"],
                size,
                cand["content_type"],
                state.revision_for(relpath, signature),
            ),
            relpath,
            signature,
        ):
            if drain:
                _drain_within_deadline()
            return
        # Advance the fairness cursor so the next heartbeat starts scanning
        # after this relpath, giving later-sorted files a chance to upload.
        state.scan_start = relpath
        if drain:
            if not _drain_within_deadline():
                return
            continue
        # One scheduling decision per heartbeat, even if a tiny local upload
        # completes before this loop reaches the next candidate.
        return


# Predecessor run-status files stripped from a hydrated resume bundle so they
# cannot contaminate THIS run's terminal detection / metric parsing. The
# checkpoint files the resumed command actually needs (weights, optimizer state,
# etc.) are kept — only Blueprint's own per-run status files are removed.
_RESUME_STALE_ARTIFACT_NAMES = frozenset(
    {"result.json", "command.log.json", "terminal.json", "live_tail.b64"}
)


def _safe_resume_members(tar: "tarfile.TarFile", dest_dir: str) -> list:
    """Return tar members that extract safely under *dest_dir*.

    Rejects absolute paths, ``..`` escapes, and non-regular/non-dir members
    (symlinks, devices, fifos) so a hostile or corrupt bundle can never write
    outside the artifact dir. The bundle is Blueprint-produced and same-project,
    but resume runs on operator hardware — fail-closed on traversal regardless.
    """
    dest_abs = os.path.abspath(dest_dir)
    safe: list = []
    for member in tar.getmembers():
        name = member.name
        if not name or name.startswith("/") or os.path.isabs(name):
            continue
        target = os.path.abspath(os.path.join(dest_dir, name))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            continue
        if not (member.isreg() or member.isdir()):
            continue
        safe.append(member)
    return safe


def _download_legacy_resume_bundle(
    url: str, token: str, bundle_path: str
) -> str | None:
    """Download an old proxied bundle within the historical compatibility cap."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/gzip"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status not in (200, 201):
                return f"http_{response.status}"
            size, _crc = _stream_response_to_file(
                response, bundle_path, max_bytes=_RUNNER_ARTIFACT_MAX_BYTES
            )
    except urllib.error.HTTPError as exc:
        return f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"download_error:{type(exc).__name__}"
    if size == 0:
        return "empty_bundle"
    if size > _RUNNER_ARTIFACT_MAX_BYTES:
        return "legacy_bundle_too_large"
    return None


def _download_input_artifact(
    orchestrator_url: str, token: str, job_id: str, artifact_dir: str
) -> dict:
    """Download, verify, safely extract, and atomically publish a checkpoint.

    New backends return a signed direct-download URL plus authoritative size and
    CRC32C metadata. The bundle is streamed to disk and verified before a single
    member is extracted. Older backends return gzip bytes directly; that
    compatibility path remains bounded by the historical 25 MiB ceiling.
    """
    parent = os.path.dirname(os.path.abspath(artifact_dir))
    os.makedirs(parent, exist_ok=True)
    fd, bundle_path = tempfile.mkstemp(prefix=".bp-resume-", suffix=".tar.gz", dir=parent)
    os.close(fd)
    broker_url = (
        f"{orchestrator_url}/api/runner/compute-job/{job_id}/input-artifact?direct=true"
    )
    legacy_url = f"{orchestrator_url}/api/runner/compute-job/{job_id}/input-artifact"
    try:
        req = urllib.request.Request(
            broker_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, application/gzip",
            },
            method="GET",
        )
        legacy_downloaded = False
        try:
            response = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                return {"ok": False, "reason": f"http_{exc.code}"}
            legacy_failure = _download_legacy_resume_bundle(
                legacy_url, token, bundle_path
            )
            if legacy_failure is not None:
                return {"ok": False, "reason": legacy_failure}
            legacy_downloaded = True
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"download_error:{type(exc).__name__}"}

        metadata = None
        if not legacy_downloaded:
            with response as resp:
                if resp.status not in (200, 201):
                    return {"ok": False, "reason": f"http_{resp.status}"}
                headers = getattr(resp, "headers", None)
                content_type = headers.get("Content-Type", "") if headers is not None else ""
                if "json" in content_type.lower():
                    raw = resp.read(64 * 1024 + 1)
                    if len(raw) > 64 * 1024:
                        return {"ok": False, "reason": "invalid_transfer_metadata"}
                    try:
                        metadata = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return {"ok": False, "reason": "invalid_transfer_metadata"}
                    if not isinstance(metadata, dict):
                        return {"ok": False, "reason": "invalid_transfer_metadata"}
                else:
                    size, _crc = _stream_response_to_file(
                        resp, bundle_path, max_bytes=_RUNNER_ARTIFACT_MAX_BYTES
                    )
                    if size == 0:
                        return {"ok": False, "reason": "empty_bundle"}
                    if size > _RUNNER_ARTIFACT_MAX_BYTES:
                        return {"ok": False, "reason": "legacy_bundle_too_large"}

        if metadata is not None:
            download_url = metadata.get("download_url")
            expected_crc = _valid_crc32c(metadata.get("crc32c"))
            try:
                expected_size = int(metadata.get("size"))
            except (TypeError, ValueError):
                expected_size = 0
            if not isinstance(download_url, str) or not download_url or expected_size <= 0 or expected_crc is None:
                legacy_failure = _download_legacy_resume_bundle(
                    legacy_url, token, bundle_path
                )
                if legacy_failure is not None:
                    return {"ok": False, "reason": legacy_failure}
                metadata = None

            download_error = "download_failed"
            for attempt in range(_REPORT_MAX_RETRIES + 1) if metadata is not None else ():
                last = attempt == _REPORT_MAX_RETRIES
                signed_req = urllib.request.Request(
                    download_url, headers={"Accept": "application/gzip"}, method="GET"
                )
                try:
                    with urllib.request.urlopen(signed_req, timeout=120) as signed_resp:
                        if signed_resp.status not in (200, 201):
                            download_error = f"download_http_{signed_resp.status}"
                        else:
                            received_size, received_crc = _stream_response_to_file(
                                signed_resp, bundle_path, max_bytes=expected_size
                            )
                            if received_size != expected_size:
                                return {"ok": False, "reason": "integrity_mismatch:size"}
                            if received_crc != expected_crc:
                                return {"ok": False, "reason": "integrity_mismatch:crc32c"}
                            break
                except urllib.error.HTTPError as exc:
                    download_error = f"download_http_{exc.code}"
                    if exc.code < 500 and exc.code != 429:
                        return {"ok": False, "reason": download_error}
                except Exception as exc:  # noqa: BLE001
                    download_error = f"download_error:{type(exc).__name__}"
                if last:
                    return {"ok": False, "reason": download_error}
                time.sleep(_report_retry_sleep(attempt))

        staging_dir = f"{artifact_dir}.resume-staging"
        shutil.rmtree(staging_dir, ignore_errors=True)
        os.makedirs(staging_dir, exist_ok=True)
        try:
            file_count, total_bytes = _extract_resume_bundle(bundle_path, staging_dir)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(staging_dir, ignore_errors=True)
            return {"ok": False, "reason": f"extract_error:{type(exc).__name__}"}
        if file_count == 0:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return {"ok": False, "reason": "no_files_after_extract"}
        try:
            os.replace(staging_dir, artifact_dir)
        except OSError as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return {"ok": False, "reason": f"publish_error:{type(exc).__name__}"}
        return {"ok": True, "file_count": file_count, "bytes": total_bytes}
    finally:
        try:
            os.remove(bundle_path)
        except OSError:
            pass


def _crc32c_table() -> tuple[int, ...]:
    table: list[int] = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
        table.append(crc & 0xFFFFFFFF)
    return tuple(table)


_CRC32C_TABLE = _crc32c_table()


def _crc32c_update(state: int, data: bytes) -> int:
    for value in data:
        state = _CRC32C_TABLE[(state ^ value) & 0xFF] ^ (state >> 8)
    return state & 0xFFFFFFFF


def _crc32c_b64(state: int) -> str:
    value = state ^ 0xFFFFFFFF
    return base64.b64encode(struct.pack(">I", value)).decode("ascii")


def _valid_crc32c(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    return value if len(decoded) == 4 else None


class _ChecksummedWriter:
    """File wrapper that counts and CRC32C-hashes compressed archive writes."""

    def __init__(self, fileobj):
        self.fileobj = fileobj
        self.size = 0
        self.crc_state = 0xFFFFFFFF

    def write(self, data: bytes) -> int:
        written = self.fileobj.write(data)
        chunk = data[:written]
        self.size += written
        self.crc_state = _crc32c_update(self.crc_state, chunk)
        return written

    def tell(self) -> int:
        return self.fileobj.tell()

    def flush(self) -> None:
        self.fileobj.flush()


def _build_artifact_archive(artifact_dir: str) -> tuple[str | None, dict]:
    if not os.path.isdir(artifact_dir) or os.path.islink(artifact_dir):
        return None, {"status": "not_produced", "reason": "artifact_dir_unavailable"}
    files: list[tuple[str, str, int]] = []
    for root, dirs, names in os.walk(artifact_dir, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            relpath = os.path.relpath(path, artifact_dir).replace(os.sep, "/")
            files.append((relpath, path, int(st.st_size)))
    if not files:
        return None, {"status": "not_produced", "reason": "no_artifact_files"}
    files.sort()
    fd, archive_path = tempfile.mkstemp(prefix=".bp-artifact-", suffix=".tar.gz")
    os.close(fd)
    try:
        with open(archive_path, "wb") as raw:
            writer = _ChecksummedWriter(raw)
            with tarfile.open(
                fileobj=typing.cast(typing.BinaryIO, writer), mode="w|gz"
            ) as tar:
                for relpath, path, _size in files:
                    tar.add(path, arcname=relpath, recursive=False)
            writer.flush()
        return archive_path, {
            "status": "prepared",
            "file_count": len(files),
            "total_bytes": sum(item[2] for item in files),
            "archive_size": writer.size,
            "crc32c": _crc32c_b64(writer.crc_state),
        }
    except Exception:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        raise


def _stream_http_file(
    url: str, method: str, headers: dict, file_path: str, file_size: int
) -> tuple[int, dict, bytes]:
    """Send a file with bounded memory and return status, headers, small body."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("unsupported transfer URL")
    connection_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_cls(parsed.hostname, parsed.port, timeout=120)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        connection.putrequest(method.upper(), target)
        for name, value in headers.items():
            if name.lower() != "content-length":
                connection.putheader(str(name), str(value))
        connection.putheader("Content-Length", str(file_size))
        connection.endheaders()
        with open(file_path, "rb") as source:
            while True:
                chunk = source.read(_ARTIFACT_TRANSFER_CHUNK_BYTES)
                if not chunk:
                    break
                connection.send(chunk)
        response = connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        body = response.read(64 * 1024)
        return response.status, response_headers, body
    finally:
        connection.close()


def _stream_response_to_file(response, path: str, *, max_bytes: int) -> tuple[int, str]:
    size = 0
    crc_state = 0xFFFFFFFF
    with open(path, "wb") as output:
        while True:
            chunk = response.read(_ARTIFACT_TRANSFER_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                return size, _crc32c_b64(crc_state)
            output.write(chunk)
            crc_state = _crc32c_update(crc_state, chunk)
    return size, _crc32c_b64(crc_state)


def _safe_resume_target(dest_dir: str, member_name: str) -> str | None:
    normalized = member_name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    dest_abs = os.path.abspath(dest_dir)
    target = os.path.abspath(os.path.join(dest_abs, *parts))
    if target != dest_abs and not target.startswith(dest_abs + os.sep):
        return None
    return target


def _extract_resume_bundle(bundle_path: str, staging_dir: str) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    with tarfile.open(bundle_path, mode="r|gz") as tar:
        for member in tar:
            target = _safe_resume_target(staging_dir, member.name)
            if target is None or not (member.isdir() or member.isreg()):
                continue
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if os.path.basename(target) in _RESUME_STALE_ARTIFACT_NAMES:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise tarfile.ExtractError("regular member has no data")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, member.mode & 0o777 or 0o600)
            written = 0
            try:
                with os.fdopen(fd, "wb", closefd=True) as output:
                    while True:
                        chunk = source.read(_ARTIFACT_TRANSFER_CHUNK_BYTES)
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
            finally:
                source.close()
            if written != member.size:
                raise tarfile.ExtractError("truncated regular member")
            file_count += 1
            total_bytes += written
    return file_count, total_bytes


def _artifact_delivery_failure(reason: str, *, protocol: str = "direct") -> dict:
    return {
        "status": "failed",
        "protocol": protocol,
        "reason": str(reason)[:_ARTIFACT_DELIVERY_REASON_MAX_CHARS],
    }


def _legacy_upload_artifact(
    orchestrator_url: str,
    token: str,
    job_id: str,
    archive_path: str,
    prepared: dict,
) -> dict:
    boundary = "----blueprintrunner" + os.urandom(16).hex()
    fd, body_path = tempfile.mkstemp(prefix=".bp-artifact-multipart-")
    os.close(fd)
    try:
        with open(body_path, "wb") as body:
            for name in ("file_count", "total_bytes"):
                body.write(f"--{boundary}\r\n".encode())
                body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
                body.write(str(prepared[name]).encode() + b"\r\n")
            body.write(f"--{boundary}\r\n".encode())
            body.write(
                b'Content-Disposition: form-data; name="archive"; '
                b'filename="artifacts.tar.gz"\r\nContent-Type: application/gzip\r\n\r\n'
            )
            with open(archive_path, "rb") as archive:
                shutil.copyfileobj(archive, body, _ARTIFACT_TRANSFER_CHUNK_BYTES)
            body.write(f"\r\n--{boundary}--\r\n".encode())
        body_size = os.path.getsize(body_path)
        url = f"{orchestrator_url}/api/runner/compute-job/{job_id}/artifact"
        for attempt in range(_REPORT_MAX_RETRIES + 1):
            try:
                status, _headers, _body = _stream_http_file(
                    url,
                    "POST",
                    {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Accept": "application/json",
                    },
                    body_path,
                    body_size,
                )
            except Exception as exc:  # noqa: BLE001
                status = 0
                reason = f"legacy_network_error:{type(exc).__name__}"
            else:
                reason = f"legacy_http_{status}"
            if 200 <= status < 300:
                return {**prepared, "status": "delivered", "protocol": "legacy", "verified": False}
            if attempt < _REPORT_MAX_RETRIES and (status == 0 or status == 429 or status >= 500):
                time.sleep(_report_retry_sleep(attempt))
                continue
            return _artifact_delivery_failure(reason, protocol="legacy")
        return _artifact_delivery_failure("legacy_upload_failed", protocol="legacy")
    finally:
        try:
            os.remove(body_path)
        except OSError:
            pass


def _upload_artifact(
    orchestrator_url: str, token: str, job_id: str, artifact_dir: str
) -> dict:
    """Persist a disk-backed artifact bundle and return an explicit outcome."""
    archive_path: str | None = None
    try:
        archive_path, prepared = _build_artifact_archive(artifact_dir)
        if archive_path is None:
            return prepared
        authorize_url = (
            f"{orchestrator_url}/api/runner/compute-job/{job_id}/artifact-transfer"
        )
        for attempt in range(_REPORT_MAX_RETRIES + 1):
            last = attempt == _REPORT_MAX_RETRIES
            try:
                auth_status, authorization = _post(
                    authorize_url,
                    token,
                    {
                        "archive_size": prepared["archive_size"],
                        "crc32c": prepared["crc32c"],
                        "file_count": prepared["file_count"],
                        "total_bytes": prepared["total_bytes"],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                auth_status = 0
                authorization = None
                failure_reason = f"authorization_error:{type(exc).__name__}"
            else:
                failure_reason = f"authorization_http_{auth_status}"

            if auth_status in (404, 405):
                if (
                    prepared["total_bytes"] <= _RUNNER_ARTIFACT_MAX_BYTES
                    and prepared["archive_size"] <= _RUNNER_ARTIFACT_MAX_BYTES
                ):
                    return _legacy_upload_artifact(
                        orchestrator_url, token, job_id, archive_path, prepared
                    )
                return _artifact_delivery_failure("direct_transfer_unsupported")
            if not (200 <= auth_status < 300) or not isinstance(authorization, dict):
                if not last and (auth_status == 0 or auth_status == 429 or auth_status >= 500):
                    time.sleep(_report_retry_sleep(attempt))
                    continue
                return _artifact_delivery_failure(failure_reason)

            upload_url = authorization.get("upload_url")
            transfer_id = authorization.get("transfer_id")
            method = str(authorization.get("method") or "PUT")
            upload_headers = authorization.get("headers") or {"Content-Type": "application/gzip"}
            if not isinstance(upload_url, str) or not isinstance(transfer_id, str) or not isinstance(upload_headers, dict):
                return _artifact_delivery_failure("invalid_authorization_response")
            try:
                put_status, put_headers, _put_body = _stream_http_file(
                    upload_url,
                    method,
                    upload_headers,
                    archive_path,
                    prepared["archive_size"],
                )
            except Exception as exc:  # noqa: BLE001
                put_status = 0
                put_headers = {}
                failure_reason = f"upload_error:{type(exc).__name__}"
            else:
                failure_reason = f"upload_http_{put_status}"
            generation = put_headers.get("x-goog-generation")
            if not (200 <= put_status < 300) or not generation:
                if not last and (put_status == 0 or put_status == 429 or put_status >= 500 or not generation):
                    time.sleep(_report_retry_sleep(attempt))
                    continue
                return _artifact_delivery_failure(
                    "upload_missing_generation" if 200 <= put_status < 300 else failure_reason
                )

            complete_url = authorize_url + "/complete"
            complete_status = 0
            completed = None
            retry_with_new_transfer = False
            for complete_attempt in range(_REPORT_MAX_RETRIES + 1):
                try:
                    complete_status, completed = _post(
                        complete_url,
                        token,
                        {"transfer_id": transfer_id, "generation": str(generation)},
                    )
                except Exception as exc:  # noqa: BLE001
                    complete_status = 0
                    failure_reason = f"completion_error:{type(exc).__name__}"
                else:
                    failure_reason = f"completion_http_{complete_status}"
                if 200 <= complete_status < 300 and isinstance(completed, dict):
                    artifact = completed.get("runner_artifact")
                    if isinstance(artifact, dict) and artifact.get("verified") is True:
                        print(
                            f"[compute] uploaded {prepared['file_count']} artifact file(s) "
                            f"({prepared['total_bytes']} bytes) for job {job_id}",
                            flush=True,
                        )
                        return {
                            **prepared,
                            "status": "delivered",
                            "protocol": "direct",
                            "verified": True,
                            "generation": str(generation),
                        }
                    return _artifact_delivery_failure("completion_unverified")
                # Transport failures and rate-limit rejections are ambiguous or
                # non-finalizing: the server may have verified this transfer, and
                # its 429 guard leaves the authorized transfer untouched. Retry the
                # idempotent completion request. A received 5xx can instead mean the
                # server finalized it as failed (notably verification_unavailable),
                # in which case completing the same transfer will only produce a
                # permanent 409. Re-authorize and upload a fresh exact object via
                # the outer retry loop.
                if complete_attempt < _REPORT_MAX_RETRIES and complete_status in (0, 429):
                    time.sleep(_report_retry_sleep(complete_attempt))
                    continue
                if complete_status >= 500 and not last:
                    retry_with_new_transfer = True
                    break
                return _artifact_delivery_failure(failure_reason)
            if retry_with_new_transfer:
                time.sleep(_report_retry_sleep(attempt))
                continue
        return _artifact_delivery_failure("transfer_retries_exhausted")
    except Exception as exc:  # noqa: BLE001 — delivery outcome, never runner crash
        reason = f"unexpected_error:{type(exc).__name__}"
        print(f"[compute] artifact upload unexpected error for job {job_id}: {exc}", flush=True)
        return _artifact_delivery_failure(reason)
    finally:
        if archive_path:
            try:
                os.remove(archive_path)
            except OSError:
                pass


def _is_safe_tar_member(name: str) -> bool:
    """True when a tar member name is safe to extract under a destination dir.

    Rejects absolute paths and any member that would escape the destination via
    ``..`` traversal. Mirrors the orchestrator-side check in
    ``compute_backends._is_safe_tar_member`` so runner and cloud rehydration reject
    the same crafted-bundle shapes.
    """
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        return False
    if os.path.splitdrive(normalized)[0]:
        return False
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    return ".." not in parts


def _rehydrate_input_artifact(
    orchestrator_url: str,
    token: str,
    job_id: str,
    artifact_dir: str,
) -> None:
    """Fetch a predecessor's durable bundle and extract it into ``artifact_dir``.

    Cross-job checkpoint resume: downloads the bundle streamed by
    ``GET /api/runner/compute-job/{job_id}/input-artifact`` (scoped server-side to
    this pinned runner + same project) and extracts it, path-traversal-safe, into
    the job's artifact dir BEFORE the command runs so the command sees the
    checkpoint already present in ``$BLUEPRINT_ARTIFACT_DIR``.

    Raises ``RuntimeError`` on any download/extract failure so the caller fails the
    job LOUDLY with a clear reason rather than running against an empty workspace.
    """
    url = f"{orchestrator_url}/api/runner/compute-job/{job_id}/input-artifact"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            bundle = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"input_artifact_rehydration_failed: HTTP {exc.code} fetching "
            f"predecessor bundle for job {job_id}"
        ) from exc
    except Exception as exc:  # network/URL errors
        raise RuntimeError(
            f"input_artifact_rehydration_failed: error fetching predecessor "
            f"bundle for job {job_id}: {exc}"
        ) from exc

    os.makedirs(artifact_dir, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
            extracted = 0
            for member in tar.getmembers():
                if member.islnk() or member.issym():
                    continue
                if not _is_safe_tar_member(member.name):
                    print(
                        f"[compute] skipping unsafe artifact-bundle member "
                        f"{member.name!r} during rehydration for job {job_id}",
                        flush=True,
                    )
                    continue
                tar.extract(member, path=artifact_dir)
                extracted += 1
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(
            f"input_artifact_rehydration_failed: could not extract predecessor "
            f"bundle for job {job_id}: {exc}"
        ) from exc
    print(
        f"[compute] rehydrated {extracted} checkpoint file(s) from predecessor "
        f"for job {job_id}",
        flush=True,
    )


def _parse_result_metrics(result_file: str) -> dict | None:
    """Read bounded scalar diagnostics from result.json (best-effort, tolerant)."""
    try:
        with open(result_file, encoding="utf-8") as fh:
            parsed = json.loads(fh.read())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    filtered = {}
    for key, value in parsed.items():
        if len(filtered) >= 256 or not isinstance(key, str):
            continue
        if isinstance(value, bool):
            filtered[key] = value
        elif isinstance(value, (int, float)) and math.isfinite(float(value)):
            filtered[key] = value
        elif isinstance(value, str):
            filtered[key] = value.encode("utf-8")[:4096].decode(
                "utf-8", errors="ignore"
            )
    return filtered or None


def _resolve_compute_cwd(
    clone_dir: str | None, requested_cwd: object
) -> str | None:
    """Resolve a job cwd inside its cloned repository.

    Compute contracts express ``cwd`` relative to the repository root. Resolve
    that path on the host before isolation planning so every backend can map the
    same directory into its sandbox. Reject traversal and symlink escapes rather
    than allowing a job-authored cwd to point elsewhere on the runner.
    """
    if clone_dir is None:
        if requested_cwd:
            raise ValueError(
                "a compute cwd requires a successfully cloned repository"
            )
        return None

    clone_mount = os.path.abspath(clone_dir)
    clone_root = os.path.realpath(clone_dir)
    raw_cwd = str(requested_cwd or ".").strip() or "."
    candidate_requested = (
        os.path.realpath(raw_cwd)
        if os.path.isabs(raw_cwd)
        else os.path.realpath(os.path.join(clone_root, raw_cwd))
    )
    try:
        contained = (
            os.path.commonpath((clone_root, candidate_requested)) == clone_root
        )
    except ValueError:
        contained = False
    if not contained:
        raise ValueError(
            f"{raw_cwd!r} escapes the cloned repository; cwd must stay within it"
        )
    if not os.path.isdir(candidate_requested):
        raise ValueError(
            f"{raw_cwd!r} does not name a directory in the cloned repository"
        )
    # Preserve the bind source's spelling (including a symlinked TMPDIR) while
    # using the canonical path only for containment validation. Isolation maps
    # cwd relative to the non-canonical bind source supplied to the runtime.
    relative = os.path.relpath(candidate_requested, clone_root)
    return os.path.normpath(os.path.join(clone_mount, relative))


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
    resource_ledger: "_ResourceLedger | None" = None,
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
    # No-progress watchdog timeout: when set, the runner monitors the progress file
    # and kills the job early if no progress signal is seen within this window.
    no_progress_timeout_seconds = job_body.get("no_progress_timeout_seconds")
    if no_progress_timeout_seconds is not None:
        no_progress_timeout_seconds = max(60, int(no_progress_timeout_seconds))
    input_artifact_job_id = job_body.get("input_artifact_job_id") or None
    require_durable_artifact = job_body.get("require_durable_artifact") is True

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
    artifact_delivery_attempted = False

    def _report_terminal(
        *,
        exec_status: str,
        exit_code: int | None = None,
        result_metrics: dict | None = None,
        command_log: dict | None = None,
        progress: dict | None = None,
        resume_hydration: dict | None = None,
    ) -> bool:
        """Persist artifacts before exposing a terminal execution report."""
        nonlocal artifact_delivery_attempted
        if artifact_dir:
            with _ComputeJobReportHeartbeat(orchestrator_url, token, job_id):
                delivery = _upload_artifact(
                    orchestrator_url, token, job_id, artifact_dir
                )
        else:
            delivery = {
                "status": "not_produced",
                "protocol": "none",
                "reason": "artifact_dir_unavailable",
            }
        artifact_delivery_attempted = True
        delivery = {**delivery, "command_exec_status": exec_status}
        reported_status = exec_status
        if require_durable_artifact and delivery.get("status") != "delivered":
            reported_status = "failed"
            print(
                f"[compute] job {job_id}: require_durable_artifact unmet "
                f"({delivery.get('reason', delivery.get('status'))})",
                flush=True,
            )
        return _report_compute_job(
            orchestrator_url,
            token,
            job_id,
            exec_status=reported_status,
            exit_code=exit_code,
            result_metrics=result_metrics,
            command_log=command_log,
            progress=progress,
            resume_hydration=resume_hydration,
            artifact_delivery=delivery,
        )


    try:
        # ── Fail-closed gate: refuse anything we cannot sandbox, before any work.
        if iso.decide_compute_backend(caps, has_image, allow_host_fallback=allow_host_fallback) == iso.DECLINE:
            reason = iso.compute_decline_reason(caps, has_image, allow_host_fallback=allow_host_fallback) or "no isolation boundary"
            print(f"[compute] DECLINED job {job_id} (fail-closed): {reason}", flush=True)
            outcome = "failure"
            _report_terminal(
                exec_status="failed", exit_code=1,
                command_log={
                    "text": f"Runner declined job (fail-closed isolation): {reason}",
                    "truncated": False,
                },
            )
            return

        if cancel_requested:
            _report_terminal(
                exec_status="failed", exit_code=1,
                command_log={
                    "text": "[runner] job was cancel-requested at claim; "
                    "command never started",
                    "truncated": False,
                },
            )
            return

        tmpdir = tempfile.mkdtemp(prefix="bp_compute_")
        result_file = os.path.join(tmpdir, "result.json")
        artifact_dir = os.path.join(tmpdir, "artifacts")
        # Interim-progress channel: the experiment may append JSONL records here
        # (via $BLUEPRINT_PROGRESS_FILE); the runner tails it each heartbeat.
        # Never pre-created — appending code creates it, absence means no progress.
        progress_file = os.path.join(tmpdir, "progress.jsonl")
        os.makedirs(artifact_dir, exist_ok=True)
        resume_hydration: dict | None = None

        # Cross-job checkpoint resume: hydrate the predecessor's durable bundle
        # into the artifact dir BEFORE any command runs (mirrors the repo-clone
        # window below), so the resumed command sees the prior checkpoint already
        # present. Fail-closed: a failed hydration aborts the job rather than
        # silently restarting from scratch (which would discard weeks of progress
        # and burn a full deadline on an unintended from-scratch run).
        if input_artifact_job_id:
            with _ComputeJobReportHeartbeat(orchestrator_url, token, job_id):
                provenance = _download_input_artifact(
                    orchestrator_url, token, job_id, artifact_dir
                )
            provenance["resumed_from_job_id"] = str(input_artifact_job_id)
            if provenance.get("ok"):
                print(
                    f"[compute] job {job_id}: resumed from {input_artifact_job_id} "
                    f"({provenance.get('file_count')} file(s), "
                    f"{provenance.get('bytes')} bytes hydrated)",
                    flush=True,
                )
                resume_hydration = provenance
                _report_compute_job(
                    orchestrator_url,
                    token,
                    job_id,
                    exec_status="running",
                    resume_hydration=provenance,
                )
            else:
                reason = provenance.get("reason", "unknown")
                print(
                    f"[compute] job {job_id}: checkpoint resume from "
                    f"{input_artifact_job_id} FAILED ({reason}); aborting job",
                    flush=True,
                )
                outcome = "failure"
                # Suppress artifact upload — predecessor fragments must not be
                # reported as a durable delivery for a job that never ran.
                artifact_dir = None
                _report_terminal(
                    exec_status="failed",
                    exit_code=1,
                    command_log={
                        "text": f"[runner] input_artifact_rehydration_failed: {reason}; "
                        "command never started",
                        "truncated": False,
                    },
                    resume_hydration=provenance,
                )
                return

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
                _report_terminal(
                    exec_status="failed", exit_code=clone_rc,
                    command_log={
                        "text": f"[runner] git clone of {owner}/{repo}@{ref} "
                        f"failed (exit={clone_rc}); command never started",
                        "truncated": False,
                    },
                    resume_hydration=resume_hydration,
                )
                return
            print(f"[compute] cloned {owner}/{repo}@{ref} for job {job_id}", flush=True)

        try:
            cwd = _resolve_compute_cwd(clone_dir, cwd)
        except ValueError as exc:
            print(
                f"[compute] invalid working directory for job {job_id}: {exc}",
                flush=True,
            )
            outcome = "failure"
            _report_terminal(
                exec_status="failed",
                exit_code=1,
                command_log={
                    "text": f"[runner] invalid compute cwd: {exc}; "
                    "command never started",
                    "truncated": False,
                },
                resume_hydration=resume_hydration,
            )
            return

        # Registry auth for docker/podman backends only (trusted host `docker login`).
        backend_pre = iso.decide_compute_backend(caps, has_image, allow_host_fallback=allow_host_fallback)
        if (
            backend_pre in (iso.BACKEND_DOCKER, iso.BACKEND_PODMAN)
            and container_registry_auth.get("password")
            and not _docker_login(container_registry_auth, container_image=container_image)
        ):
            outcome = "failure"
            _report_terminal(
                exec_status="failed", exit_code=1,
                command_log={
                    "text": "[runner] docker login to the container registry "
                    f"failed for image {container_image!r}; command never started",
                    "truncated": False,
                },
            )
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
            progress_file_host=progress_file,
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
        # NOTE: BLUEPRINT_PROGRESS_FILE is NOT set here. plan_compute_execution
        # already injects it into plan.env with the correct IN-SANDBOX path
        # (translated from progress_file_host to the per-backend mount, e.g.
        # /tmp/blueprint_out, /workspace, or SANDBOX_WORKSPACE). Overriding it
        # with the host-absolute `progress_file` would point the command at a
        # path that is not mounted inside the container, so the host would never
        # observe progress writes (breaking both the progress-tail protocol and
        # the no-progress watchdog, which stat() the host `progress_file`).
        if plan.declined:
            print(f"[compute] DECLINED job {job_id} (fail-closed): {plan.decline_reason}", flush=True)
            outcome = "failure"
            _report_terminal(
                exec_status="failed", exit_code=1,
                command_log={
                    "text": f"Runner declined job (fail-closed isolation): {plan.decline_reason}",
                    "truncated": False,
                },
                resume_hydration=resume_hydration,
            )
            return

        # Cache hygiene bookkeeping: stamp this image as USED now. Docker keeps no
        # last-use time, so without this stamp the low-disk prune could only see
        # the image's upstream CREATION date and would evict a base image this job
        # just pulled (see _prune_docker_images). Attribute the pull to the
        # owning project so a multi-project runner's heartbeat can report which
        # project's images fill its (small) image-layer disk.
        _record_image_use(container_image, project_id=job_body.get("project_id"))

        # GPU-used enforcement: run a lightweight nvidia-smi -L probe INSIDE the
        # same sandbox before the real command, so (a) torch/TF cannot silently
        # fall back to CPU if CUDA is not actually reachable, and (b) an image
        # whose CUDA runtime cannot use this GPU's arch (NGC images drop older
        # compute capabilities) is caught before the FULL RUN is burned. It does
        # NOT save the image pull — the pull happens first, by necessity.
        #
        # A container image alone arms the gate: the image/GPU arch mismatch
        # exists whenever GPUs meet a container, even with no declared
        # accelerator. An imageless (bwrap) GPU job still probes only when an
        # accelerator was declared, exactly as before.
        if assigned_gpus and (accelerator_type or container_image):
            # Pull FIRST. The probe's own `docker run` would otherwise BE the
            # pull (docker's default policy is `missing`), pitting the probe's
            # 30s timeout against a cold multi-GB download it cannot win — the
            # job would then die reporting a probe timeout instead of the real
            # reason. Best-effort: a pull failure is left for the probe / real
            # run to surface, so there is only one authoritative failure path.
            pull_ok, pull_detail = iso.pull_container_image(plan, container_image)
            if not pull_ok:
                print(
                    f"[compute] job {job_id}: pre-probe image pull did not "
                    f"succeed ({pull_detail}) — continuing; the probe or the run "
                    "will surface the real error",
                    flush=True,
                )

            probe_ok, probe_detail = iso.probe_gpu_in_sandbox(plan)
            if not probe_ok:
                # An arch mismatch is CONCLUSIVE: the container itself reported it
                # cannot use this GPU, so the run can only end in "no metric".
                # Fatal no matter what the job declared.
                #
                # Every OTHER probe failure (timeout, no nvidia-smi, empty device
                # list) is INCONCLUSIVE — it says the PROBE could not run, not
                # that the JOB cannot. Killing on those would be a false failure,
                # and the blast radius is wide: `assigned_gpus` is populated from
                # caps.has_gpu ALONE, so on a GPU host it covers every
                # containerized job, CPU-only ones included. Keep the old contract
                # — only a job that DECLARED an accelerator dies on an
                # inconclusive probe; an image-only job warns and proceeds.
                arch_mismatch = iso.no_usable_gpu_marker(probe_detail) is not None
                if not arch_mismatch and not accelerator_type:
                    print(
                        f"[compute] job {job_id}: GPU probe inconclusive "
                        f"({probe_detail}) — no accelerator was declared, so the "
                        "job proceeds rather than fail on a probe it never needed",
                        flush=True,
                    )
                else:
                    declared = (
                        f"accelerator={accelerator_type}"
                        if accelerator_type
                        else f"image={container_image}"
                    )
                    terminal_reason = (
                        f"GPU assigned ({declared}) but unusable "
                        f"in sandbox: {probe_detail}"
                    )
                    print(f"[compute] ABORT job {job_id}: {terminal_reason}", flush=True)
                    outcome = "failure"
                    _report_terminal(
                        exec_status="failed", exit_code=1,
                        command_log={
                            "text": terminal_reason,
                            "truncated": False,
                        },
                        resume_hydration=resume_hydration,
                    )
                    return
            else:
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
            _report_terminal(
                exec_status="failed", exit_code=1,
                command_log={
                    "text": f"[runner] failed to start command: {exc}",
                    "truncated": False,
                },
                resume_hydration=resume_hydration,
            )
            return

        print(
            f"[compute] started job {job_id} pid={proc.pid} backend={plan.backend} "
            f"gpus={assigned_gpus}",
            flush=True,
        )
        _report_compute_job(
            orchestrator_url,
            token,
            job_id,
            exec_status="running",
            resume_hydration=resume_hydration,
        )

        # Capture stdout with dedup + head/tail budget (see _BoundedDedupLog).
        log_capture = _BoundedDedupLog(
            _RUNNER_MAX_COMMAND_LOG_BYTES, _RUNNER_COMMAND_LOG_HEAD_BYTES
        )

        # Bounded tail of the job's self-reported progress records; polled each
        # heartbeat interval and attached to interim AND terminal reports so a
        # deadline-killed 24h run still surfaces its training trajectory.
        progress_tail = _ProgressTail(progress_file)

        # Mid-run viewable streaming state: tracks which artifact files have been
        # pushed (by relpath + mtime/size signature) so plots/videos render live
        # in the strategic canvas while the job is still running (best-effort).
        viewable_state = _ViewableStreamState()

        def _interim_log_tail() -> dict | None:
            # Live-output tail (~4KB) for interim running reports. The server
            # stamps it as polling_metadata["live_output"] (same shape as the
            # GCP live-tail path); the FULL bounded log still travels on the
            # terminal report only. truncated=True whenever the tail is a
            # window on a longer capture.
            log_text, truncated = log_capture.result()
            if not log_text:
                return None
            tail = log_text[-_RUNNER_LIVE_TAIL_CHARS:]
            return {
                "text": tail,
                "truncated": truncated or len(tail) < len(log_text),
            }

        def _terminal_command_log(kill_note: str | None = None) -> dict | None:
            # EVERY terminal report must carry the captured log: the deadline/
            # cancel/shutdown kill paths previously reported failed with NO
            # command_log, discarding the output of exactly the most expensive
            # failures (a 24h run killed at its deadline). ``kill_note`` lands
            # at the TAIL so a tail-truncated view still ends with WHY.
            log_text, truncated = log_capture.result()
            if image_downgraded:
                log_text = (
                    f"NOTE: container_image {container_image} requested but no "
                    "container runtime available on this host; running "
                    "image-less on host toolchain.\n"
                ) + log_text
            if kill_note:
                sep = "" if (not log_text or log_text.endswith("\n")) else "\n"
                log_text = f"{log_text}{sep}[runner] {kill_note}\n"
            if not log_text and not truncated:
                return None
            return {
                "text": log_text,
                "truncated": truncated,
                "byte_count": len(log_text.encode("utf-8")),
            }

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
        # No-progress watchdog: track the last time the progress file was touched.
        # Initial state is "progress at start" so the window begins from launch.
        _np_last_progress = start_time
        _np_last_mtime: float | None = None

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
                _report_terminal(
                    exec_status="failed", exit_code=124,
                    command_log=_terminal_command_log(
                        "killed by runner: runtime deadline "
                        f"({runtime_deadline_seconds}s) exceeded"
                    ),
                    progress=progress_tail.poll(),
                    resume_hydration=resume_hydration,
                )
                return

            # No-progress watchdog: check if the progress file has been touched
            # since the last observation. Any mtime bump counts as progress.
            if no_progress_timeout_seconds is not None:
                try:
                    _cur_mtime = os.stat(progress_file).st_mtime
                except OSError:
                    _cur_mtime = None
                # Any advance counts as progress, INCLUDING the first appearance of
                # the file (transition from "no file yet" to a valid mtime). Gating
                # on `_np_last_mtime is not None` would miss that first write and
                # measure the window from job start instead of from first progress.
                if _cur_mtime is not None and (
                    _np_last_mtime is None or _cur_mtime > _np_last_mtime
                ):
                    _np_last_progress = now
                _np_last_mtime = _cur_mtime
                if now - _np_last_progress >= no_progress_timeout_seconds:
                    _teardown()
                    outcome = "failure"
                    _report_terminal(
                        exec_status="failed", exit_code=125,
                        command_log=_terminal_command_log(
                            "killed by runner: no-progress watchdog "
                            f"({no_progress_timeout_seconds}s without a progress "
                            "update) exceeded"
                        ),
                        progress=progress_tail.poll(),
                        resume_hydration=resume_hydration,
                    )
                    return

            if _shutdown_event.is_set():
                _teardown()
                _report_terminal(
                    exec_status="failed", exit_code=1,
                    command_log=_terminal_command_log(
                        "killed by runner: runner shutting down"
                    ),
                    progress=progress_tail.poll(),
                    resume_hydration=resume_hydration,
                )
                return

            _omissions: dict = {}
            if viewable_state.oversize_relpaths:
                _omissions["size_cap"] = len(viewable_state.oversize_relpaths)
            if viewable_state.count_cap_relpaths:
                _omissions["count_cap"] = len(viewable_state.count_cap_relpaths)
            cancel = _report_compute_job(
                orchestrator_url, token, job_id,
                exec_status="running",
                command_log=_interim_log_tail(),
                progress=progress_tail.poll(),
                result_metrics=_parse_result_metrics(result_file),
                resume_hydration=resume_hydration,
                viewable_omissions=_omissions or None,
            )
            if cancel:
                _teardown()
                _report_terminal(
                    exec_status="failed", exit_code=1,
                    command_log=_terminal_command_log(
                        "killed by runner: cancel requested by orchestrator"
                    ),
                    progress=progress_tail.poll(),
                    resume_hydration=resume_hydration,
                )
                return

            # Mid-run: push NEW-or-CHANGED viewable files (reward-curve plots,
            # rollout frames/videos) so they render live in the strategic canvas
            # instead of only at job completion. Best-effort — the terminal tarball
            # upload below is unchanged, and the server dedups terminal against
            # mid-run by matching storage object paths.
            _stream_viewable_files(
                orchestrator_url, token, job_id, artifact_dir, viewable_state
            )

        # Process exited: let the reader drain, then close our read-fd so a leftover
        # reader (backgrounded child still holding the pipe) is unblocked and the fd
        # is never leaked across the runner's lifetime.
        reader.join(timeout=5)
        _close_stdout()
        reader.join(timeout=1)

        # A command can create its final plot or rollout video immediately before
        # exiting, without waiting long enough to reach the next heartbeat. Flush
        # once while the job is still RUNNING: the viewable endpoint rejects
        # terminal jobs, and the durable artifact bundle may be skipped when it
        # exceeds its much smaller upload cap. Reuse the live state so already
        # uploaded files are not sent again.
        with _ComputeJobReportHeartbeat(
            orchestrator_url, token, job_id
        ) as final_viewable_heartbeat:
            _stream_viewable_files(
                orchestrator_url,
                token,
                job_id,
                artifact_dir,
                viewable_state,
                drain=True,
            )
        if final_viewable_heartbeat.cancel_requested:
            outcome = None
            _report_terminal(
                exec_status="failed",
                exit_code=1,
                command_log=_terminal_command_log(
                    "cancel requested by orchestrator during final viewable upload"
                ),
                progress=progress_tail.poll(),
            )
            return

        result_metrics = _parse_result_metrics(result_file)

        command_log = _terminal_command_log()

        succeeded = exit_code_val == 0
        outcome = "success" if succeeded else "failure"
        exec_status_final = "succeeded" if succeeded else "failed"
        _report_terminal(
            exec_status=exec_status_final,
            exit_code=exit_code_val,
            result_metrics=result_metrics,
            command_log=command_log,
            progress=progress_tail.poll(),
            resume_hydration=resume_hydration,
        )
        print(f"[compute] job {job_id} command {exec_status_final} (exit={exit_code_val})",
              flush=True)

    except Exception as exc:
        print(f"[compute] unexpected error for job {job_id}: {exc}", flush=True)
        outcome = "failure"
        try:
            try:
                # Salvage whatever the command printed before the runner crashed.
                crash_log = _terminal_command_log(f"unexpected runner error: {exc}")
            except NameError:
                # Crashed before the log capture existed — the exception text is
                # the only evidence there is.
                crash_log = {
                    "text": f"[runner] unexpected runner error: {exc}",
                    "truncated": False,
                }
            _report_terminal(
                exec_status="failed", exit_code=1, command_log=crash_log
            )
        except Exception:
            pass
    finally:
        # Record health telemetry for this job (neutral outcomes leave it untouched).
        if telemetry is not None:
            if outcome == "success":
                telemetry.record_success()
            elif outcome == "failure":
                telemetry.record_failure()
        # A terminal report always calls _report_terminal first. If execution
        # escaped without one, retain the bundle locally until cleanup but do not
        # create unreported durable evidence here.
        if artifact_dir and not artifact_delivery_attempted:
            print(
                f"[compute] job {job_id}: terminal report was not reached; "
                "artifact delivery was not attempted",
                flush=True,
            )
        _release_gpus(gpu_state, assigned_gpus)
        # Keep the durable permit and the active set in the same visibility
        # order. A new claim for this job ID must never be able to rename a new
        # permit before this completed worker releases its old one.
        if resource_ledger is not None:
            resource_ledger.release(f"compute:{job_id}")
        with lock:
            active_jobs.discard(job_id)
        _release_capacity(capacity)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        # Cache hygiene: Docker layers live under the runtime data-root, which
        # can be distinct from the job workspace. Do not run Docker cleanup for
        # a Podman store: it cannot free Podman's graph-root filesystem.
        try:
            image_store = _container_image_store(caps)
            if image_store is None or image_store.get("runtime") == iso.BACKEND_DOCKER:
                prune_root = (
                    image_store.get("root") if image_store else _workspace_root()
                )
                prune_floor = (
                    _container_image_reclaim_min_free_bytes()
                    if image_store is not None
                    else _min_free_disk_bytes()
                )
                prune_result = _prune_docker_images(prune_root, prune_floor)
                if image_store is not None and telemetry is not None:
                    telemetry.record_container_image_reclaim(
                        image_store, prune_result
                    )
                if prune_result.get("pruned"):
                    print(
                        f"[compute] job {job_id}: Docker image hygiene (low disk) "
                        f"keep_days={prune_result.get('keep_days')} "
                        f"removed={prune_result.get('removed')} "
                        f"kept={prune_result.get('kept')} "
                        f"in_use_skipped={prune_result.get('in_use_skipped')}",
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
    live_config: "_LiveRunnerConfig | None" = None,
    resource_ledger: "_ResourceLedger | None" = None,
) -> None:
    """Background thread: poll for compute jobs and spawn a worker per claimed job.

    The loop body is wrapped so a transient error never silently kills the poll
    thread (which would strand compute capacity); the ``main()`` supervisor makes
    an actually-dead thread fatal.
    """
    url = f"{orchestrator_url}/api/runner/next-compute-job"
    poll_interval = base_poll_interval
    last_low_disk_reclaim_at = float("-inf")

    while not _shutdown_event.is_set():
        capacity_acquired = False
        reservation_key: str | None = None
        durable_reservation_key: str | None = None
        claimed_job_id: str | None = None
        try:
            if _update_in_progress.is_set():
                time.sleep(poll_interval)
                continue

            with lock:
                active_count = len(active_jobs)

            # A live server config push can grow/shrink the compute ceiling
            # mid-flight; read it each iteration so the gate is the source of
            # truth (the static `max_concurrent` is only the env-var default).
            effective_max = (
                live_config.max_concurrent_compute()
                if live_config is not None
                else max_concurrent
            )
            if active_count >= effective_max:
                time.sleep(poll_interval)
                continue

            if not _try_acquire_capacity(capacity):
                time.sleep(poll_interval)
                continue
            capacity_acquired = True

            # Keep workspace pressure and container-image pressure separate. An
            # imageless/bwrap job only needs workspace scratch, while a Docker
            # image pull consumes the runtime data-root. Reclaim stale Docker
            # images even when the server correctly refuses to hand us an
            # under-floor image job; otherwise no claim means no cleanup path.
            workspace_root = _workspace_root()
            min_free_disk_bytes = _min_free_disk_bytes()
            workspace_low = _disk_below_min(workspace_root, min_free_disk_bytes)
            image_store = _container_image_store(capabilities)
            image_store_min_free_bytes = _container_image_reclaim_min_free_bytes()
            image_store_low = bool(
                image_store
                and image_store.get("runtime") == iso.BACKEND_DOCKER
                and _disk_below_min(
                    image_store.get("root"), image_store_min_free_bytes
                )
            )
            reclaim_store = image_store if image_store_low else None
            reclaim_min_free_bytes = (
                image_store_min_free_bytes if image_store_low else min_free_disk_bytes
            )
            if reclaim_store is None and workspace_low and image_store is None:
                # Older/docker-info-unavailable runners retain the prior best-
                # effort workspace-root cleanup behavior.
                reclaim_store = {
                    "runtime": iso.BACKEND_DOCKER,
                    "root": workspace_root,
                    "free_bytes": _disk_free_bytes(workspace_root),
                }
            if reclaim_store is not None:
                reclaim_root = reclaim_store.get("root")
                now_monotonic = time.monotonic()
                if (
                    isinstance(reclaim_root, str)
                    and now_monotonic - last_low_disk_reclaim_at >= 60.0
                ):
                    last_low_disk_reclaim_at = now_monotonic
                    try:
                        prune_result = _prune_docker_images(
                            reclaim_root, reclaim_min_free_bytes
                        )
                        if telemetry is not None:
                            telemetry.record_container_image_reclaim(
                                reclaim_store, prune_result
                            )
                        prune_detail = (
                            prune_result.get("reason")
                            or prune_result.get("output")
                            or "completed"
                        )
                        print(
                            "[compute] low-disk pre-claim image-store hygiene "
                            f"({prune_detail})",
                            flush=True,
                        )
                    except Exception as exc:
                        print(
                            "[compute] low-disk pre-claim image-store hygiene "
                            f"failed: {exc}",
                            flush=True,
                        )
            if _disk_below_min(workspace_root, min_free_disk_bytes):
                _release_capacity(capacity)
                capacity_acquired = False
                print(
                    f"[compute] low disk on workspace {workspace_root} "
                    f"(< {min_free_disk_bytes} bytes free) — skipping compute claim.",
                    flush=True,
                )
                time.sleep(poll_interval)
                continue

            # Host-load guard: skip claiming when GPU is saturated.
            if max_gpu_util_pct > 0:
                util = _gpu_utilization_pct()
                if util is not None and util >= max_gpu_util_pct:
                    _release_capacity(capacity)
                    capacity_acquired = False
                    print(
                        f"[compute] gpu_utilization_pct {util} >= threshold {max_gpu_util_pct}"
                        " — skipping compute claim",
                        flush=True,
                    )
                    time.sleep(poll_interval)
                    continue

            reservation_key = f"compute:claim:{secrets.token_hex(8)}"
            if resource_ledger is not None:
                rejection = resource_ledger.try_reserve(reservation_key, "compute")
                if rejection is not None:
                    _release_capacity(capacity)
                    capacity_acquired = False
                    print(
                        f"[compute] resource admission rejected ({rejection}) — "
                        "skipping compute claim",
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
                if resource_ledger is not None:
                    resource_ledger.release(reservation_key)
                reservation_key = None
                _release_capacity(capacity)
                capacity_acquired = False
                print(f"[compute] poll error: {exc}", flush=True)
                time.sleep(5)
                continue

            if status == 429:
                if resource_ledger is not None:
                    resource_ledger.release(reservation_key)
                reservation_key = None
                # Rate limited: honor the server's Retry-After instead of a fixed
                # sleep so we back off exactly as long as asked (and no longer).
                wait = retry_after if retry_after is not None else min(poll_interval * 2, 30.0)
                wait = max(0.0, min(wait, 300.0))
                print(f"[compute] poll 429 — backing off {wait:.1f}s (Retry-After)", flush=True)
                _release_capacity(capacity)
                capacity_acquired = False
                time.sleep(wait)
                continue

            if status == 401:
                if resource_ledger is not None:
                    resource_ledger.release(reservation_key)
                reservation_key = None
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
                capacity_acquired = False
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status == 204 or body is None:
                if resource_ledger is not None:
                    resource_ledger.release(reservation_key)
                reservation_key = None
                _release_capacity(capacity)
                capacity_acquired = False
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status != 200:
                if resource_ledger is not None:
                    resource_ledger.release(reservation_key)
                reservation_key = None
                _release_capacity(capacity)
                capacity_acquired = False
                print(f"[compute] poll warning: HTTP {status}", flush=True)
                time.sleep(5)
                continue

            poll_interval = base_poll_interval

            job_id = str(body.get("job_id", "unknown"))
            with lock:
                if job_id in active_jobs:
                    print(f"[compute] skipping duplicate job {job_id}", flush=True)
                    if resource_ledger is not None:
                        resource_ledger.release(reservation_key)
                    reservation_key = None
                    _release_capacity(capacity)
                    capacity_acquired = False
                    time.sleep(poll_interval)
                    continue
                active_jobs.add(job_id)
            claimed_job_id = job_id
            if resource_ledger is not None:
                resource_ledger.rename(reservation_key, f"compute:{job_id}")
                durable_reservation_key = f"compute:{job_id}"
            reservation_key = None

            print(f"[compute] claimed job {job_id}", flush=True)
            try:
                worker = threading.Thread(
                    target=_run_compute_job,
                    args=(body, orchestrator_url, token, active_jobs, lock, capacity),
                    kwargs={
                        "capabilities": capabilities,
                        "gpu_state": gpu_state,
                        "telemetry": telemetry,
                        "resource_ledger": resource_ledger,
                    },
                    daemon=True,
                )
                worker.start()
            except Exception as exc:
                # Do this before exposing the ID as inactive: a replacement
                # claim must not be released by this failed-start cleanup.
                if resource_ledger is not None:
                    resource_ledger.release(f"compute:{job_id}")
                with lock:
                    active_jobs.discard(job_id)
                _release_capacity(capacity)
                capacity_acquired = False
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
            else:
                # The worker now owns both the semaphore and durable permit.
                capacity_acquired = False
                durable_reservation_key = None
                claimed_job_id = None
        except Exception as exc:  # noqa: BLE001 — keep the poll thread alive
            if reservation_key is not None and resource_ledger is not None:
                resource_ledger.release(reservation_key)
            if durable_reservation_key is not None and resource_ledger is not None:
                resource_ledger.release(durable_reservation_key)
            if claimed_job_id is not None:
                with lock:
                    active_jobs.discard(claimed_job_id)
            if capacity_acquired:
                _release_capacity(capacity)
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


def _gpu_sandbox_smoke(caps: "iso.Capabilities", allowlist: list) -> str:
    """Run the preflight GPU probe in a REAL sandbox workspace; return one line.

    The probe re-runs the plan's argv verbatim, and that argv binds
    ``workspace_host`` into the sandbox — bwrap fails hard ("Can't find source
    path") when a bind source is missing. The workspace therefore MUST still
    exist while the probe runs: the temp dir is torn down in a ``finally`` AFTER
    the probe, never between the plan and the probe (which made the preflight
    smoke fail 100% of the time on a real GPU host, reporting a sandbox CUDA
    outage that did not exist).
    """
    smoke_indices = list(allowlist)[:1]
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="bp_gpu_smoke_")
        result_file = os.path.join(tmpdir, "result.json")
        artifact_dir = os.path.join(tmpdir, "artifacts")
        os.makedirs(artifact_dir, exist_ok=True)
        ldconfig = _read_ldconfig() if iso.BACKEND_BWRAP else ""
        plan = iso.plan_compute_execution(
            caps=caps,
            command="nvidia-smi -L",
            image=None,
            registry_auth=None,
            workspace_host=tmpdir,
            result_file_host=result_file,
            artifact_dir_host=artifact_dir,
            clone_dir_host=None,
            cwd_host=None,
            job_env={},
            gpu_indices=smoke_indices,
            container_name="bp-smoke",
            source_env=dict(os.environ),
            ldconfig_output=ldconfig,
        )
        if plan.declined:
            return f"plan declined: {plan.decline_reason}"
        # Workspace still on disk here — the probe binds it.
        probe_ok, probe_detail = iso.probe_gpu_in_sandbox(plan)
        if probe_ok:
            first_line = (probe_detail or "").splitlines()
            return f"CUDA reachable: {first_line[0] if first_line else probe_detail}"
        return f"WARNING — CUDA unreachable in sandbox: {probe_detail}"
    except Exception as exc:  # noqa: BLE001 — a smoke error must not fail --check
        return f"smoke check error: {exc}"
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _earn_project_verification(
    caps: "iso.Capabilities",
    agent_binary: list[str] | None,
    agent_image: str | None,
) -> dict:
    """Probe the write-agent boundary once; never change general availability."""
    try:
        with tempfile.TemporaryDirectory(prefix="blueprint-verification-") as workspace:
            shutil.copy2(
                iso.PROJECT_VERIFICATION_PROBE_PATH,
                os.path.join(workspace, "project-verification-probe.sh"),
            )
            plan = iso.plan_agent_execution(
                caps=caps,
                agent_binary=agent_binary,
                workspace_host=workspace,
                injected_env={"TASK_ID": "project-verification-probe"},
                source_env=dict(os.environ),
                task_type="fix",
                agent_image=agent_image,
            )
            return iso.probe_project_verification(plan)
    except Exception as exc:  # noqa: BLE001 — readiness proof is fail-closed evidence
        reason = f"verification probe setup failed: {str(exc)[:300]}"
        failed_probe = {"ok": False, "exit_code": None, "reason": reason}
        return {
            "version": iso.PROJECT_VERIFICATION_VERSION,
            "boundary": {"class": iso.CLASS_AGENT, "backend": iso.DECLINE},
            "ready": False,
            "reason": reason,
            "probes": {
                "ruff": dict(failed_probe),
                "docker_postgres": dict(failed_probe),
            },
        }


def _preflight(
    orchestrator_url: str,
    token: str,
    agent_binary: list[str] | None,
    caps: "iso.Capabilities",
    gpu_state: dict,
    max_concurrent: int,
    max_concurrent_compute: int,
    agent_image: str | None = None,
    project_verification: dict | None = None,
    resource_ledger: "_ResourceLedger | None" = None,
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
        heartbeat_body = {
            # Preflight probe, NOT a genuine active-work report: this empty
            # active set must NOT overwrite a live report on a box that is
            # already running a real runner. active_work_report=False makes
            # the orchestrator ignore the active set here, so the reaper's
            # zombie reconcile cannot mistake this probe for "no work claimed"
            # and reap a live task (reaper_orphan_classify).
            "active_task_ids": [],
            "active_compute_job_ids": [],
            "active_work_report": False,
            "agent_available": claimable[iso.CLASS_AGENT],
            "supported_agent_task_types": supported_agent_task_types,
            "hostname": hostname,
            "version": _runner_version(),
            # Isolation-backend inventory + selected backends so `--check`
            # populates the fleet view the same way the running heartbeat does.
            "isolation_backends": iso.capabilities_inventory(caps),
            "container_gpu_passthrough": caps.container_gpu_passthrough,
            "agent_backend": iso.decide_agent_backend(
                caps, agent_binary_present=agent_binary is not None,
                agent_image=agent_image,
            ),
            "compute_backend": iso.decide_compute_backend(
                caps, has_image=agent_image is not None
            ),
            **gpu_caps,
        }
        if project_verification is not None:
            heartbeat_body["project_verification"] = project_verification
        status, ping_body = _post(
            f"{orchestrator_url}/api/runner/heartbeat",
            token,
            heartbeat_body,
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
        "(separate policy pools; shared resource admission prevents host overcommit)",
    )
    if resource_ledger is not None:
        capacity = resource_ledger.snapshot(gpu_state)
        _fmt(
            "resource admission",
            f"{'enabled' if capacity['enabled'] else 'DISABLED'}  "
            f"cpu={capacity['cpu']['free_cores']}/{capacity['cpu']['total_cores']} free cores  "
            f"ram={capacity['memory']['free_bytes']}/{capacity['memory']['total_bytes']} free bytes",
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
        _fmt("gpu sandbox", _gpu_sandbox_smoke(caps, allowlist))
    elif caps.has_gpu:
        _fmt("gpu sandbox", "skipped (bwrap not usable)")

    for note in caps.notes:
        print(f"    note         : {note}", flush=True)

    if project_verification is not None:
        verification_state = (
            "READY (Ruff + live sandbox PostgreSQL passed in agent boundary)"
            if project_verification.get("ready") is True
            else f"INCAPABLE — {project_verification.get('reason') or 'probe failed'}"
        )
        _fmt("verification", verification_state)

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
    # The report prints the REAL state, including the "systemd is PID 1 but this
    # runner is not a systemd unit" case that used to read as healthy.
    supervisor_state = {
        "supervisor": None,
        "supervised": False,
        "evidence": "supervisor detection failed",
        "detail": "could not determine supervision state — assume unsupervised",
    }
    try:
        supervisor_state = _detect_supervisor_state()
    except Exception as exc:  # pragma: no cover - defensive
        supervisor_state["evidence"] = f"supervisor detection error: {exc}"
    supervisor = supervisor_state["supervisor"] if supervisor_state["supervised"] else None
    if supervisor is not None:
        # The detector confirms a systemd *service* unit but cannot read its
        # Restart= policy. Update-survival (the runner exits 0 after a self-update
        # and trusts systemd to relaunch it) needs Restart=always — Restart=on-failure
        # would leave the clean exit-0 dead. `blueprint-runner install` ships always,
        # so surface the requirement rather than assume a hand-rolled unit provides it.
        note = (
            " — needs Restart=always (blueprint-runner install sets it)"
            if supervisor == "systemd"
            else ""
        )
        _fmt(
            "supervisor",
            f"{supervisor} (auto-restart on crash/update{note}) "
            f"[{supervisor_state['evidence']}]",
        )
    else:
        print(
            "  supervisor      : NONE — UNSUPERVISED\n"
            f"                    {supervisor_state['evidence']}\n"
            f"                    {supervisor_state['detail']}.\n"
            "                    The runner recovers from AUTO-UPDATES via in-place\n"
            "                    re-exec, but a CRASH will NOT restart it. Run it as a\n"
            "                    systemd service (Restart=always), under supervisord, or\n"
            "                    in a restart loop with BLUEPRINT_RUNNER_SUPERVISED=1.\n"
            "                    See RUNNER_RUNBOOK (--help).",
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
        "supervised": bool(supervisor_state["supervised"]),
        "supervisor_evidence": supervisor_state["evidence"],
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


_BWRAP_CREDENTIAL_PATH_ENV_KEYS = (
    "CODEX_HOME",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


def _relocate_bwrap_credential_env(llm_env: dict[str, str]) -> dict[str, str]:
    """Point provider credential paths at their writable workspace copies.

    The backend emits cloud-image paths such as ``/opt/agent/openai``.  A local
    runner writes the corresponding file below its per-task workspace, which is
    mounted at ``/workspace`` inside bwrap—not at the sandbox root.  Preserve
    every non-path credential unchanged and relocate only known absolute path
    variables so Codex/Gemini read the files the runner actually materialized.
    """
    relocated = dict(llm_env)
    for key in _BWRAP_CREDENTIAL_PATH_ENV_KEYS:
        value = relocated.get(key)
        if isinstance(value, str) and value.startswith("/"):
            relocated[key] = f"{iso.SANDBOX_WORKSPACE}/{value.lstrip('/')}"
    return relocated


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
    project_id: str | None = None,
    llm_env: dict[str, str] | None = None,
    llm_files: list[dict[str, str]] | None = None,
    decline_reason_out: list[str] | None = None,
) -> bool:
    """Plan (fail-closed) + spawn one claimed agent task; record it for reaping.

    On a False (declined / could-not-spawn) return, the WHY is appended to
    ``decline_reason_out`` when the caller supplies a list, so it can be sent to
    ``/api/runner/decline-task`` instead of being printed and dropped — an
    orchestrator that cannot see the reason cannot stop re-pinning the task to
    the very runner that keeps rejecting it. An out-param (rather than a richer
    return type) keeps the ``bool`` contract every existing caller relies on.

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
    def _decline(reason: str) -> bool:
        """Print the reason locally AND hand it back to the caller (→ orchestrator)."""
        print(
            f"[runner] DECLINED agent task {task_id} (fail-closed): {reason}",
            flush=True,
        )
        if decline_reason_out is not None:
            decline_reason_out.append(reason)
        return False

    if backend == iso.DECLINE:
        # No isolation boundary for THIS task type on this host (fail-closed).
        return _decline(
            iso.agent_decline_reason(caps, present) or "no agent sandbox"
        )
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
        if decline_reason_out is not None:
            decline_reason_out.append(
                "agent binary unavailable on this runner (bwrap backend needs the "
                "host agent dist)"
            )
        return False

    # Only hand the image to the planner when THIS task's decision is docker; a
    # bwrap-eligible task on a docker+image box still takes the bwrap path.
    plan_image = agent_image if backend == iso.BACKEND_DOCKER else None

    effective_llm_env = dict(llm_env or {})
    if backend == iso.BACKEND_BWRAP:
        effective_llm_env = _relocate_bwrap_credential_env(effective_llm_env)

    # Write backend-supplied credential files into the agent's workspace
    # (mounted at /workspace inside the bwrap sandbox). The LLM provider CLI
    # (Claude, Codex,
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
    if effective_llm_env:
        injected_env.update(effective_llm_env)
    # Task identity is authoritative and must not be overridden by provider
    # credential material returned in credentials.env.
    if project_id:
        injected_env["PROJECT_ID"] = project_id
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
        return _decline(plan.decline_reason or "sandbox plan declined")

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
        if decline_reason_out is not None:
            decline_reason_out.append(
                f"agent process spawn failed: {type(exc).__name__}: {exc}"[:500]
            )
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

    if br.livekit_gateway_role_is_selected():
        print(
            "ERROR: this host is reserved for the Blueprint LiveKit gateway role; "
            "refusing to start task capacity. Stop and remove the gateway role "
            "explicitly before running local_runner.py.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    check_mode = "--check" in sys.argv[1:]

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # In --check we AGGREGATE every missing prerequisite into this list and report
    # them all in one pass at the end, instead of hard-exiting on the first miss —
    # so an operator fixes the whole list at once rather than one-fix-rerun at a
    # time. Normal boot keeps its fail-fast semantics (first missing → exit 1).
    missing_prereqs: list[str] = []

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
                "  Re-enroll: open the Runtime tab → Devices and runners in the admin UI and click Enroll runner.",
                file=sys.stderr,
            )
            print(
                f"  (credential file checked: {cred_path})",
                file=sys.stderr,
            )
            if not check_mode:
                sys.exit(1)
            # --check: record it and press on with an empty token so the preflight
            # probes (isolation/capability/supervisor) still run and print.
            missing_prereqs.append("BLUEPRINT_RUNNER_TOKEN")
            token = ""
        else:
            print(
                f"[runner] token loaded from credential file ({_credential_path()})",
                flush=True,
            )
    if check_mode:
        orchestrator_url = os.environ.get("BLUEPRINT_ORCHESTRATOR_URL", "").strip().rstrip("/")
        if not orchestrator_url:
            print(
                "ERROR: required environment variable BLUEPRINT_ORCHESTRATOR_URL is not set.",
                file=sys.stderr,
            )
            print(
                "  Set it and re-run: export BLUEPRINT_ORCHESTRATOR_URL=<value>",
                file=sys.stderr,
            )
            missing_prereqs.append("BLUEPRINT_ORCHESTRATOR_URL")
    else:
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

    # Activate previously verified Blueprint-owned host CLIs before any host
    # preflight/probe. Maintain them asynchronously so npm/registry trouble can
    # never take healthy Claude, Gemini, compute, or heartbeat capacity offline.
    # A genuinely Docker-only runner is image-managed and does not mutate host
    # dependencies. Hybrid Docker+bwrap runners still maintain the host CLIs
    # needed by their write/heavy bwrap task slice.
    host_agent_backend = _host_cli_maintenance_allowed(caps, agent_image)
    if host_agent_backend:
        _activate_managed_cli_paths()
        if not check_mode:
            threading.Thread(
                target=_host_cli_maintenance_loop,
                name="blueprint-cli-maintenance",
                daemon=True,
            ).start()

    # Resolve after managed PATH activation so bwrap binds the verified release.
    agent_binary = _locate_agent_binary()

    # Earn authoring-verification capability once at startup through the exact
    # write-agent boundary. This is intentionally independent of generic agent
    # and compute availability: read-only work and compute stay claimable when
    # Ruff or Docker/Postgres verification is unavailable.
    project_verification = _earn_project_verification(
        caps, agent_binary, agent_image
    )

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
    resource_ledger = _ResourceLedger(_workspace_root())

    # Best-effort startup sweep of any stray tagged containers from a prior crash.
    iso.sweep_stale_containers(caps)

    result = _preflight(
        orchestrator_url, token, agent_binary, caps, gpu_state,
        max_concurrent, max_concurrent_compute, agent_image,
        project_verification, resource_ledger,
    )
    claimable = result["claimable"]
    gpu_capabilities = result["gpu_caps"]
    agent_claimable = claimable[iso.CLASS_AGENT]
    compute_claimable = claimable[iso.CLASS_COMPUTE]

    if check_mode:
        # Claim no work; exit reflects whether the box is ready. Surface EVERY
        # missing prerequisite together (not one-at-a-time) so the operator can
        # fix the whole list in a single pass.
        if missing_prereqs:
            print(
                "\n[runner] --check found "
                f"{len(missing_prereqs)} missing prerequisite(s): "
                + ", ".join(missing_prereqs),
                file=sys.stderr,
            )
            print(
                "[runner] Set the variable(s) above and re-run `--check`.",
                file=sys.stderr,
            )
            sys.exit(1)
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

    # NOTE: the env-var BLUEPRINT_RUNNER_MAX_CONCURRENT is only the INITIAL
    # default. The heartbeat RESPONSE carries a server-authoritative live-safe
    # config block (max_concurrent / max_concurrent_compute / task_types /
    # gpu_indices_allowed) that OVERRIDES it on the next tick — no warn-only
    # mismatch path any more, and no SSH/restart to reconfigure a live runner.

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
    # agent pool can't starve compute (or vice-versa). Sized to a HARD CEILING
    # (never below the env default or the server's 1-64 live-safe bound) so the
    # semaphore never becomes the binding constraint when a live config push
    # GROWS the limit — the `active_count >= live_limit` gate below is the real
    # source of truth the claim loops read each tick.
    agent_capacity = threading.BoundedSemaphore(
        max(max_concurrent, _LiveRunnerConfig.MAX_CONCURRENT)
    )
    compute_capacity = threading.BoundedSemaphore(
        max(max_concurrent_compute, _LiveRunnerConfig.MAX_CONCURRENT)
    )
    # Live-safe config holder, seeded from the env-var startup values. The
    # heartbeat loop overwrites its live-safe fields when the server sends a
    # config block; the agent + compute claim loops read the live limit each
    # iteration. "all" GPU sentinel maps to every DETECTED index (caps.gpu_indices).
    live_config = _LiveRunnerConfig(
        max_concurrent,
        max_concurrent_compute,
        task_types="all",
        gpu_state=gpu_state,
        all_gpu_indices=list(caps.gpu_indices or []),
    )
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
            "resource_ledger": resource_ledger,
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
            # Live-safe config channel: the heartbeat loop applies the server's
            # config block and echoes applied-vs-pending back.
            "live_config": live_config,
            "project_verification": project_verification,
            "resource_ledger": resource_ledger,
            "gpu_state": gpu_state,
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
                    "max_gpu_util_pct": max_gpu_util_pct,
                    "live_config": live_config,
                    "resource_ledger": resource_ledger},
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
        reservation_key: str | None = None
        task_reservation_key: str | None = None
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

            # A live server config push can grow/shrink the agent ceiling
            # mid-flight; read it each iteration so the gate is the source of
            # truth (the static `max_concurrent` is only the env-var default).
            if active_count >= live_config.max_concurrent():
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

            reservation_key = f"agent:claim:{secrets.token_hex(8)}"
            rejection = resource_ledger.try_reserve(reservation_key, "agent")
            if rejection is not None:
                _release_capacity(agent_capacity)
                print(
                    f"[runner] resource admission rejected ({rejection}) — "
                    "skipping agent claim",
                    flush=True,
                )
                time.sleep(poll_interval)
                continue

            try:
                resp = _post(next_task_url, token, {}, return_headers=True)
                if isinstance(resp, tuple) and len(resp) == 3:
                    status, body, retry_after = resp
                else:
                    status, body = resp
                    retry_after = None
            except Exception as exc:
                resource_ledger.release(reservation_key)
                _release_capacity(agent_capacity)
                print(f"[runner] poll error: {exc}", flush=True)
                time.sleep(5)
                continue

            if status == 429:
                wait = retry_after if retry_after is not None else min(poll_interval * 2, 30.0)
                wait = max(0.0, min(wait, 300.0))
                print(f"[runner] poll 429 — backing off {wait:.1f}s (Retry-After)", flush=True)
                resource_ledger.release(reservation_key)
                _release_capacity(agent_capacity)
                time.sleep(wait)
                continue

            if status == 401:
                print(
                    "[runner] poll 401 — runner token rejected; backing off and retrying "
                    "(row may be reactivating via heartbeat).",
                    flush=True,
                )
                resource_ledger.release(reservation_key)
                _release_capacity(agent_capacity)
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            # Any 503 means "nothing dispatchable, back off" — match on the STATUS,
            # not on a body key, so a new orchestrator reason shape can never make
            # this branch miss and leak the capacity permit acquired above.
            if status == 503 and isinstance(body, dict):
                resource_ledger.release(reservation_key)
                _release_capacity(agent_capacity)
                reason = body.get("reason") or "no reason reported by orchestrator"
                if body.get("credential_resolution_failed"):
                    # The offending task id is nested under "error" so that no
                    # top-level "task_id" can ever be mistaken for a dispatched
                    # task by a runner that skipped the pool_exhausted check.
                    err = body.get("error")
                    err = err if isinstance(err, dict) else {}
                    bad_task = err.get("skipped_task_id") or "unknown"
                    print(
                        f"[runner] CREDENTIAL RESOLUTION FAILED for task "
                        f"{bad_task} — the orchestrator could not build "
                        f"credentials for this task (this is a task/config problem, "
                        f"NOT rate limiting): {reason}",
                        flush=True,
                    )
                else:
                    print(
                        f"[runner] POOL EXHAUSTED — no LLM account available; "
                        f"task stays claimable for a future poll cycle: {reason}",
                        flush=True,
                    )
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status == 204 or body is None:
                resource_ledger.release(reservation_key)
                _release_capacity(agent_capacity)
                poll_interval = min(poll_interval * 2, 30.0)
                time.sleep(poll_interval)
                continue

            if status != 200:
                resource_ledger.release(reservation_key)
                _release_capacity(agent_capacity)
                print(f"[runner] poll warning: HTTP {status}", flush=True)
                time.sleep(5)
                continue

            poll_interval = base_poll_interval

            task_id = body.get("task_id") or body.get("id", "unknown")
            project_id = body.get("project_id")
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
                resource_ledger.release(reservation_key)
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
                resource_ledger.release(reservation_key)
                _release_capacity(agent_capacity)
                # Decline explicitly: without this the task stays pinned in
                # PROVISIONING until this runner goes offline, even though we
                # know right now that we cannot run it.
                _notify_decline(
                    orchestrator_url,
                    token,
                    task_id,
                    reason=f"workspace preparation failed: {type(exc).__name__}: {exc}",
                )
                time.sleep(poll_interval)
                continue

            resource_ledger.rename(reservation_key, f"agent:{task_id}")
            task_reservation_key = f"agent:{task_id}"
            reservation_key = None

            decline_reason: list[str] = []
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
                project_id=project_id,
                llm_env=llm_env,
                llm_files=llm_files,
                decline_reason_out=decline_reason,
            )
            if not spawned:
                # DECLINE / spawn failure — the reaper never sees this task, so the
                # permit must be released here.
                resource_ledger.release(f"agent:{task_id}")
                task_reservation_key = None
                _release_capacity(agent_capacity)
                # Notify the orchestrator so the task returns to QUEUED instead of
                # stranding in PROVISIONING until the runner goes offline — WITH the
                # reason, so it stops re-pinning the task to this same runner.
                _notify_decline(
                    orchestrator_url,
                    token,
                    task_id,
                    reason=decline_reason[0] if decline_reason else None,
                )
                time.sleep(poll_interval)
                continue
        except Exception as exc:  # noqa: BLE001 — a transient error must not crash the runner
            if reservation_key is not None:
                resource_ledger.release(reservation_key)
            if task_reservation_key is not None:
                resource_ledger.release(task_reservation_key)
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
