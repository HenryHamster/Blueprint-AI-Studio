"""Blueprint local fleet runner.

Pull-based runner process for operator hardware. Polls the cloud orchestrator
for pinned tasks and spawns the Blueprint agent binary for each claimed task.

Usage:
    python3 scripts/local_runner.py

Required environment variables:
    BLUEPRINT_RUNNER_TOKEN      Token from POST /api/admin/projects/{id}/runners/register
    BLUEPRINT_ORCHESTRATOR_URL  Base URL, e.g. https://api.example.com

Optional environment variables:
    BLUEPRINT_RUNNER_MAX_CONCURRENT   Max simultaneous agent subprocesses (default: 2)
    BLUEPRINT_AGENT_BINARY            Path to node agent entry point (auto-detected if absent)
    BLUEPRINT_RUNNER_POLL_INTERVAL    Base poll interval in seconds (default: 2)
    BLUEPRINT_RUNNER_WORKSPACE_ROOT   Parent directory for per-task agent workspaces
                                      (default: <tmp>/blueprint-agent-workspaces)
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
"""

import io
import json
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
import time
import urllib.error
import urllib.request
from collections import deque

import runner_isolation as iso


# ── Configuration ────────────────────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: required environment variable {name} is not set.", file=sys.stderr)
        print(f"  Set it and re-run: export {name}=<value>", file=sys.stderr)
        sys.exit(1)
    return value


def _locate_agent_binary() -> str | None:
    """Locate the node agent entry point, or return None if it is unavailable.

    Non-fatal counterpart to ``_find_agent_binary``.  The runner main loop uses
    this to resolve the agent LAZILY — only when an AGENT task is actually
    claimed — so a compute/GPU-only runner can boot and run compute jobs without
    a built agent.  A missing binary is therefore not an error at startup; it is
    only a problem the moment an agent task needs to run.
    """
    explicit = os.environ.get("BLUEPRINT_AGENT_BINARY", "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "..", "agent", "dist", "index.js"),
        os.path.join(os.getcwd(), "agent", "dist", "index.js"),
    ]
    for path in candidates:
        normalized = os.path.normpath(path)
        if os.path.isfile(normalized):
            return normalized
    return None


def _find_agent_binary() -> str:
    """Resolve the agent entry point or exit(1) (strict, hard-fail variant).

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
        "ERROR: could not find agent/dist/index.js. "
        "Build the agent first (cd agent && npm run build) "
        "or set BLUEPRINT_AGENT_BINARY to its path.",
        file=sys.stderr,
    )
    sys.exit(1)


def _agent_workspace_for_task(task_id: str) -> str:
    """Return a per-task workspace path for an agent subprocess.

    The node agent defaults to /tmp/workspace when AGENT_WORKSPACE is absent.
    A persistent local runner may execute multiple tasks across one machine
    session, so each task gets a stable isolated workspace directory.
    """
    root = os.environ.get("BLUEPRINT_RUNNER_WORKSPACE_ROOT", "").strip()
    if not root:
        root = os.path.join(tempfile.gettempdir(), "blueprint-agent-workspaces")
    safe_task_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in task_id)
    if not safe_task_id:
        safe_task_id = "unknown"
    workspace = os.path.abspath(os.path.join(root, safe_task_id))
    os.makedirs(workspace, exist_ok=True)
    return workspace


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _post(url: str, token: str, body: dict) -> tuple[int, dict | None]:
    """POST JSON body; returns (status_code, parsed_body_or_None)."""
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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            raw = resp.read()
            try:
                return status, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return status, None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, None
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
# v1 cap on durable artifact uploads. The runner skips (does not fail) any run
# whose artifact dir exceeds this; the backend mirrors the cap and 413s.
_RUNNER_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB


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

def _detect_gpu() -> dict:
    """Detect GPU hardware by probing nvidia-smi.

    Returns a dict with has_gpu, gpu_count, and gpu_names.  Any error
    (binary absent, timeout, non-zero exit) is treated as no GPU present so
    detection is always non-fatal.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {"has_gpu": False, "gpu_count": 0, "gpu_names": []}
        import re
        gpu_names = re.findall(r"^GPU \d+:\s*(.+?)(?:\s*\(UUID:.+)?\s*$", result.stdout, re.MULTILINE)
        if gpu_names:
            return {"has_gpu": True, "gpu_count": len(gpu_names), "gpu_names": gpu_names}
        return {"has_gpu": False, "gpu_count": 0, "gpu_names": []}
    except Exception:
        return {"has_gpu": False, "gpu_count": 0, "gpu_names": []}


# ── Graceful shutdown ─────────────────────────────────────────────────────────

_shutdown_event = threading.Event()


def _handle_signal(signum, frame):  # noqa: ARG001
    if not _shutdown_event.is_set():
        print("\n[runner] Signal received — initiating graceful shutdown.", flush=True)
        _shutdown_event.set()


# ── Active task tracking (background thread) ──────────────────────────────────

def _release_capacity(capacity: threading.Semaphore | None) -> None:
    if capacity is not None:
        capacity.release()


def _try_acquire_capacity(capacity: threading.Semaphore | None) -> bool:
    if capacity is None:
        return True
    return capacity.acquire(blocking=False)


def _reap_loop(
    active_tasks: dict,
    lock: threading.Lock,
    capacity: threading.Semaphore | None = None,
) -> None:
    while not _shutdown_event.is_set():
        with lock:
            done = [tid for tid, proc in active_tasks.items() if proc.poll() is not None]
            for tid in done:
                code = active_tasks[tid].returncode
                del active_tasks[tid]
                _release_capacity(capacity)
                print(f"[DONE] task_id={tid} exit_code={code}", flush=True)
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
) -> None:
    url = f"{orchestrator_url}/api/runner/heartbeat"
    while not _shutdown_event.is_set():
        try:
            with lock:
                task_ids = list(active_tasks.keys())
            if active_compute_jobs is not None and compute_lock is not None:
                with compute_lock:
                    compute_job_ids = sorted(active_compute_jobs)
            else:
                compute_job_ids = []
            # FAIL-CLOSED advertisement: only advertise agent capability when an
            # agent SANDBOX can be built (bwrap+userns) AND the binary exists. If
            # not, agent_available=False and the orchestrator stops pinning agent
            # tasks here — the runner never claims work it cannot isolate.
            body = {
                "active_task_ids": task_ids,
                "active_compute_job_ids": compute_job_ids,
                "agent_available": agent_sandbox_ok and _locate_agent_binary() is not None,
                **gpu_capabilities,
            }
            status, _ = _post(url, token, body)
            if status not in (200, 204):
                print(f"[runner] heartbeat warning: HTTP {status}", flush=True)
        except Exception as exc:
            print(f"[runner] heartbeat error: {exc}", flush=True)
        # Sleep in 1s increments so shutdown propagates quickly
        for _ in range(30):
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
    """POST a status report for a compute job. Returns cancel_requested."""
    url = f"{orchestrator_url}/api/runner/compute-job/{job_id}/report"
    body: dict = {"exec_status": exec_status}
    if exit_code is not None:
        body["exit_code"] = exit_code
    if result_metrics is not None:
        body["result_metrics"] = result_metrics
    if command_log is not None:
        body["command_log"] = command_log
    try:
        status, resp = _post(url, token, body)
        if status == 404:
            return True  # job gone, treat as cancel
        if isinstance(resp, dict):
            return bool(resp.get("cancel_requested", False))
    except Exception as exc:
        print(f"[compute] report error for job {job_id}: {exc}", flush=True)
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
        except urllib.error.HTTPError as exc:
            print(f"[compute] artifact upload failed (HTTP {exc.code}) for job {job_id}", flush=True)
        except Exception as exc:
            print(f"[compute] artifact upload network error for job {job_id}: {exc}", flush=True)
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

    caps = capabilities if capabilities is not None else _get_capabilities()
    cpu_limit = os.environ.get("BLUEPRINT_RUNNER_CPU_LIMIT") or None
    mem_limit = os.environ.get("BLUEPRINT_RUNNER_MEM_LIMIT") or None
    has_image = bool(container_image)

    tmpdir: str | None = None
    artifact_dir: str | None = None
    assigned_gpus: list[int] = []
    plan = None
    proc = None
    try:
        # ── Fail-closed gate: refuse anything we cannot sandbox, before any work.
        if iso.decide_compute_backend(caps, has_image) == iso.DECLINE:
            reason = iso.compute_decline_reason(caps, has_image) or "no isolation boundary"
            print(f"[compute] DECLINED job {job_id} (fail-closed): {reason}", flush=True)
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
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=clone_rc)
                return
            if not cwd:
                cwd = clone_dir
            print(f"[compute] cloned {owner}/{repo}@{ref} for job {job_id}", flush=True)

        # Registry auth for docker/podman backends only (trusted host `docker login`).
        backend_pre = iso.decide_compute_backend(caps, has_image)
        if (
            backend_pre in (iso.BACKEND_DOCKER, iso.BACKEND_PODMAN)
            and container_registry_auth.get("password")
            and not _docker_login(container_registry_auth, container_image=container_image)
        ):
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
        )
        if plan.declined:
            print(f"[compute] DECLINED job {job_id} (fail-closed): {plan.decline_reason}", flush=True)
            _report_compute_job(
                orchestrator_url, token, job_id,
                exec_status="failed", exit_code=1,
                command_log={
                    "text": f"Runner declined job (fail-closed isolation): {plan.decline_reason}",
                    "truncated": False,
                },
            )
            return

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
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                log_capture.feed(chunk)

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()

        start_time = time.time()
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
            reader.join(timeout=2)

        exit_code_val: int | None = None
        while True:
            try:
                exit_code_val = proc.wait(timeout=_RUNNER_HEARTBEAT_INTERVAL)
                break  # process finished
            except subprocess.TimeoutExpired:
                pass

            now = time.time()
            if deadline is not None and now >= deadline:
                _teardown()
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

        reader.join(timeout=5)

        result_metrics = _parse_result_metrics(result_file)

        log_text, truncated = log_capture.result()
        command_log: dict | None = None
        if log_text or truncated:
            command_log = {
                "text": log_text,
                "truncated": truncated,
                "byte_count": len(log_text.encode("utf-8")),
            }

        exec_status_final = "succeeded" if exit_code_val == 0 else "failed"
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
        try:
            _report_compute_job(orchestrator_url, token, job_id,
                                 exec_status="failed", exit_code=1)
        except Exception:
            pass
    finally:
        # Upload durable artifacts BEFORE tearing down tmpdir (every terminal path).
        if artifact_dir:
            _upload_artifact(orchestrator_url, token, job_id, artifact_dir)
        _release_gpus(gpu_state, assigned_gpus)
        with lock:
            active_jobs.discard(job_id)
        _release_capacity(capacity)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


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
) -> None:
    """Background thread: poll for compute jobs and spawn a worker per claimed job."""
    url = f"{orchestrator_url}/api/runner/next-compute-job"
    poll_interval = base_poll_interval

    while not _shutdown_event.is_set():
        with lock:
            active_count = len(active_jobs)

        if active_count >= max_concurrent:
            time.sleep(poll_interval)
            continue

        if not _try_acquire_capacity(capacity):
            time.sleep(poll_interval)
            continue

        try:
            status, body = _post(url, token, {})
        except Exception as exc:
            _release_capacity(capacity)
            print(f"[compute] poll error: {exc}", flush=True)
            time.sleep(5)
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
                kwargs={"capabilities": capabilities, "gpu_state": gpu_state},
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


def _compute_backend_summary(caps: "iso.Capabilities") -> str:
    """Human summary of which compute backends this host can build."""
    parts: list[str] = []
    container = caps.preferred_container()
    if container:
        parts.append(f"{container} (image jobs, --nv/--gpus)")
    if caps.bwrap_ok:
        parts.append("bwrap (imageless + image-downgrade)")
    return ", ".join(parts) if parts else "none"


def _preflight(
    orchestrator_url: str,
    token: str,
    agent_binary: str | None,
    caps: "iso.Capabilities",
    gpu_state: dict,
    max_concurrent: int,
    max_concurrent_compute: int,
) -> dict:
    """Print the aligned preflight block; return a dict of the decisions.

    Performs the single authoritative orchestrator/token ping (a real heartbeat)
    so ``--check`` and normal boot share one code path.
    """
    hostname = socket.gethostname()
    token_prefix = token[:8] + "..." if len(token) >= 8 else token
    workspace_root = _workspace_root()
    ws_writable = _workspace_writable(workspace_root)

    gpu_indices_str = ",".join(str(i) for i in caps.gpu_indices) or "none"
    allowlist = gpu_state.get("allowed") or []
    allowlist_str = ",".join(str(i) for i in allowlist) or "none"

    claimable = iso.claimable_classes(caps, agent_binary is not None)
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
    try:
        status, _ = _post(
            f"{orchestrator_url}/api/runner/heartbeat",
            token,
            {
                "active_task_ids": [],
                "active_compute_job_ids": [],
                "agent_available": claimable[iso.CLASS_AGENT],
                **gpu_caps,
            },
        )
        orchestrator_ok = True
        if status in (200, 204):
            token_valid = True
        elif status == 401:
            token_401 = True
    except Exception as exc:
        ping_error = str(exc)

    def _fmt(label: str, value: str) -> None:
        print(f"  {label:<16}: {value}", flush=True)

    print("Blueprint local fleet runner — preflight", flush=True)
    _fmt("runner host", hostname)
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
    _fmt("concurrency", f"agents={max_concurrent}  compute={max_concurrent_compute}")

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
    for note in caps.notes:
        print(f"    note         : {note}", flush=True)

    print("  task classes:", flush=True)
    if claimable[iso.CLASS_AGENT]:
        print("    agent        : CLAIMED via bwrap", flush=True)
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
        "setup_ok": setup_ok,
    }


def main() -> None:
    check_mode = "--check" in sys.argv[1:]

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    token = _require_env("BLUEPRINT_RUNNER_TOKEN")
    orchestrator_url = _require_env("BLUEPRINT_ORCHESTRATOR_URL").rstrip("/")
    agent_binary = _locate_agent_binary()

    max_concurrent = int(os.environ.get("BLUEPRINT_RUNNER_MAX_CONCURRENT", "2"))
    max_concurrent_compute = int(
        os.environ.get("BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS", "2")
    )
    base_poll_interval = float(os.environ.get("BLUEPRINT_RUNNER_POLL_INTERVAL", "2"))

    caps = _get_capabilities()
    gpu_state = _make_gpu_state(caps)

    # Best-effort startup sweep of any stray tagged containers from a prior crash.
    iso.sweep_stale_containers(caps)

    result = _preflight(
        orchestrator_url, token, agent_binary, caps, gpu_state,
        max_concurrent, max_concurrent_compute,
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

    if not (agent_claimable or compute_claimable):
        print(
            "[runner] No task class is claimable on this host (no isolation boundary). "
            "Staying online (heartbeat only) — install bubblewrap/apptainer and restart "
            "to enable work.",
            flush=True,
        )

    # Shared state
    active_tasks: dict[str, subprocess.Popen] = {}
    lock = threading.Lock()
    runner_capacity = threading.BoundedSemaphore(max_concurrent)

    active_compute_jobs: set[str] = set()
    compute_lock = threading.Lock()

    reaper = threading.Thread(
        target=_reap_loop,
        args=(active_tasks, lock, runner_capacity),
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
            caps.bwrap_ok,  # agent_sandbox_ok — gates advertised agent_available
        ),
        daemon=True,
    )
    heartbeat.start()

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
                runner_capacity,
            ),
            kwargs={"base_poll_interval": base_poll_interval, "capabilities": caps,
                    "gpu_state": gpu_state},
            daemon=True,
        )
        compute_poll.start()

    poll_interval = base_poll_interval
    next_task_url = f"{orchestrator_url}/api/runner/next-task"

    # Agent poll loop — only runs when an agent sandbox can be built (fail-closed).
    # Otherwise we idle on the shutdown event while compute/heartbeat continue.
    while not _shutdown_event.is_set():
        if not caps.bwrap_ok:
            # No agent sandbox — never claim agent work. Wait for shutdown.
            time.sleep(1)
            continue

        with lock:
            active_count = len(active_tasks)

        if active_count >= max_concurrent:
            time.sleep(poll_interval)
            continue

        if not _try_acquire_capacity(runner_capacity):
            time.sleep(poll_interval)
            continue

        try:
            status, body = _post(next_task_url, token, {})
        except Exception as exc:
            _release_capacity(runner_capacity)
            print(f"[runner] poll error: {exc}", flush=True)
            time.sleep(5)
            continue

        if status == 401:
            print(
                "[runner] poll 401 — runner token rejected; backing off and retrying "
                "(row may be reactivating via heartbeat).",
                flush=True,
            )
            _release_capacity(runner_capacity)
            poll_interval = min(poll_interval * 2, 30.0)
            time.sleep(poll_interval)
            continue

        if status == 204 or body is None:
            _release_capacity(runner_capacity)
            poll_interval = min(poll_interval * 2, 30.0)
            time.sleep(poll_interval)
            continue

        if status != 200:
            _release_capacity(runner_capacity)
            print(f"[runner] poll warning: HTTP {status}", flush=True)
            time.sleep(5)
            continue

        poll_interval = base_poll_interval

        task_id = body.get("task_id") or body.get("id", "unknown")
        agent_token = body.get("agent_token", "")
        task_type = body.get("task_type", "unknown")
        vm_instance_id = body.get("vm_instance_id") or "runner:local"

        with lock:
            already_active = task_id in active_tasks
        if already_active:
            print(f"[runner] skipping duplicate dispatch for task {task_id}", flush=True)
            _release_capacity(runner_capacity)
            time.sleep(poll_interval)
            continue

        if not agent_binary:
            agent_binary = _locate_agent_binary()
        if not agent_binary:
            print(
                f"[runner] agent binary unavailable — cannot run agent task {task_id}; "
                "skipping (build it with `cd agent && npm run build` or set "
                "BLUEPRINT_AGENT_BINARY). Compute jobs are unaffected.",
                flush=True,
            )
            _release_capacity(runner_capacity)
            time.sleep(poll_interval)
            continue

        try:
            workspace = _agent_workspace_for_task(task_id)
        except Exception as exc:
            print(f"[runner] failed to prepare workspace for task {task_id}: {exc}", flush=True)
            _release_capacity(runner_capacity)
            time.sleep(poll_interval)
            continue

        # Build the FAIL-CLOSED sandbox plan. The env is assembled from an explicit
        # allowlist (never os.environ.copy) and NEVER carries BLUEPRINT_RUNNER_TOKEN
        # / BLUEPRINT_ORCHESTRATOR_URL into the task.
        plan = iso.plan_agent_execution(
            caps=caps,
            agent_binary=agent_binary,
            workspace_host=workspace,
            injected_env={
                "AGENT_SECRET_TOKEN": agent_token,
                "ORCHESTRATOR_URL": orchestrator_url,
                "TASK_ID": task_id,
                "VM_INSTANCE_ID": vm_instance_id,
            },
            source_env=dict(os.environ),
        )
        if plan.declined:
            print(
                f"[runner] DECLINED agent task {task_id} (fail-closed): {plan.decline_reason}",
                flush=True,
            )
            _release_capacity(runner_capacity)
            time.sleep(poll_interval)
            continue

        popen_kwargs: dict = {"env": plan.env, "stdout": sys.stdout, "stderr": sys.stderr}
        if hasattr(os, "setsid"):
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(plan.argv, **popen_kwargs)
        except Exception as exc:
            print(f"[runner] failed to spawn agent for task {task_id}: {exc}", flush=True)
            _release_capacity(runner_capacity)
            continue

        with lock:
            active_tasks[task_id] = proc
        print(
            f"[START] task_id={task_id} type={task_type} vm={vm_instance_id} "
            f"workspace={workspace} pid={proc.pid} sandbox={plan.backend}",
            flush=True,
        )

    # Graceful shutdown
    with lock:
        remaining = dict(active_tasks)

    if remaining:
        print(f"[runner] Shutdown requested — waiting for {len(remaining)} active agents...", flush=True)
        deadline = time.time() + 120
        for tid, proc in remaining.items():
            timeout = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                print(f"[runner] timeout waiting for task {tid} (pid={proc.pid}) — tearing down group", flush=True)
                iso.terminate_process_tree(proc)

    print("[runner] Exiting.", flush=True)


if __name__ == "__main__":
    main()
