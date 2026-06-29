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
"""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque


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
            body = {
                "active_task_ids": task_ids,
                "active_compute_job_ids": compute_job_ids,
                "agent_available": _locate_agent_binary() is not None,
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


def _detect_runner_gpu() -> bool:
    """Return True if a local NVIDIA GPU is present via nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


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


def _run_compute_job(
    job_body: dict,
    orchestrator_url: str,
    token: str,
    active_jobs: set,
    lock: threading.Lock,
    capacity: threading.Semaphore | None = None,
) -> None:
    """Worker thread: execute a claimed compute job and report the result."""
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

    tmpdir = tempfile.mkdtemp(prefix="bp_compute_")
    artifact_dir: str | None = None  # referenced in finally for durable-artifact upload
    try:
        result_file = os.path.join(tmpdir, "result.json")
        artifact_dir = os.path.join(tmpdir, "artifacts")
        os.makedirs(artifact_dir, exist_ok=True)

        # Build env for host-side commands (setup_step + non-containerized main)
        env = os.environ.copy()
        env.update(env_override)
        env["BLUEPRINT_RESULT_FILE"] = result_file
        env["BLUEPRINT_ARTIFACT_DIR"] = artifact_dir

        if cancel_requested:
            _report_compute_job(orchestrator_url, token, job_id,
                                 exec_status="failed", exit_code=1)
            return

        # Clone the project repo so compute commands can import project packages.
        # Mirrors the git-clone step that GCP/local-subprocess backends embed in
        # their startup scripts via _attach_repo_clone.
        clone_dir: str | None = None
        if repo_clone and repo_token:
            owner = str(repo_clone.get("owner") or "")
            repo = str(repo_clone.get("repo") or "")
            ref = str(repo_clone.get("ref") or "main")
            clone_dir = os.path.join(tmpdir, "repo")
            clone_url = f"https://x-access-token:{repo_token}@github.com/{owner}/{repo}.git"
            try:
                clone_result = subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", ref, clone_url, clone_dir],
                    capture_output=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                print(f"[compute] git clone timed out for job {job_id}", flush=True)
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=124)
                return
            except Exception as exc:
                print(f"[compute] git clone error for job {job_id}: {exc}", flush=True)
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=1)
                return
            if clone_result.returncode != 0:
                stderr_text = clone_result.stderr.decode("utf-8", "replace")[:500]
                print(f"[compute] git clone failed for job {job_id}: {stderr_text}", flush=True)
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed",
                                     exit_code=clone_result.returncode)
                return
            # Add the cloned repo root to PYTHONPATH so `import <package>` works.
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{clone_dir}:{existing_pythonpath}" if existing_pythonpath else clone_dir
            )
            # Default cwd to clone root when not explicitly specified.
            if not cwd:
                cwd = clone_dir
            print(f"[compute] cloned {owner}/{repo}@{ref} for job {job_id}", flush=True)

        # Run setup_step (always bare bash, never containerized — mirrors GCP path)
        if setup_step:
            try:
                setup_result = subprocess.run(
                    ["bash", "-lc", setup_step],
                    env=env,
                    cwd=cwd,
                    capture_output=True,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=124)
                return
            except Exception as exc:
                print(f"[compute] setup_step error for job {job_id}: {exc}", flush=True)
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=1)
                return
            if setup_result.returncode != 0:
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed",
                                     exit_code=setup_result.returncode)
                return

        # Build command args — containerized or bare
        if container_image:
            # Authenticate with the registry before pulling, if credentials provided.
            # Pass container_image so the registry host can be derived when the
            # auth payload omits an explicit server (e.g. NGC nvcr.io images).
            if container_registry_auth.get("password"):
                if not _docker_login(container_registry_auth, container_image=container_image):
                    _report_compute_job(orchestrator_url, token, job_id,
                                        exec_status="failed", exit_code=1)
                    return

            gpus = _detect_runner_gpu()
            container_result_file = f"{_RUNNER_CONTAINER_OUT_DIR}/result.json"
            container_artifact_dir = f"{_RUNNER_CONTAINER_OUT_DIR}/artifacts"

            cmd_args: list = ["docker", "run", "--rm"]
            if gpus:
                cmd_args += ["--gpus", "all"]
            cmd_args += [
                "-v", f"{tmpdir}:{_RUNNER_CONTAINER_OUT_DIR}",
                "-e", f"BLUEPRINT_RESULT_FILE={container_result_file}",
                "-e", f"BLUEPRINT_ARTIFACT_DIR={container_artifact_dir}",
            ]
            for k, v in env_override.items():
                cmd_args += ["-e", f"{k}={v}"]
            if clone_dir:
                # Bind-mount the cloned repo as /workspace and put it on PYTHONPATH
                # inside the container — mirrors the local-subprocess container path.
                cmd_args += ["-v", f"{clone_dir}:/workspace", "-e", "PYTHONPATH=/workspace"]
            effective_cwd = "/workspace" if clone_dir else cwd
            if effective_cwd:
                cmd_args += ["-w", effective_cwd]
            cmd_args += [container_image, "bash", "-lc", command]
            proc_env = None  # docker runs in runner's inherited env
            proc_cwd = None  # cwd is passed via -w into the container
        else:
            cmd_args = ["bash", "-lc", command]
            proc_env = env
            proc_cwd = cwd

        try:
            proc = subprocess.Popen(
                cmd_args,
                env=proc_env,
                cwd=proc_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:
            print(f"[compute] failed to start job {job_id}: {exc}", flush=True)
            _report_compute_job(orchestrator_url, token, job_id,
                                 exec_status="failed", exit_code=1)
            return

        print(f"[compute] started job {job_id} pid={proc.pid}", flush=True)
        _report_compute_job(orchestrator_url, token, job_id, exec_status="running")

        # Capture stdout in a background thread with consecutive-duplicate-line
        # collapsing + a head+tail budget (see _BoundedDedupLog), so spammy
        # repeated lines (e.g. CUDA ptx warnings) don't crowd out real output and
        # the END of the log — where results/summaries live — always survives.
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

        # Main wait loop: send periodic heartbeats and handle deadline/cancel
        start_time = time.time()
        deadline = (
            start_time + float(runtime_deadline_seconds)
            if runtime_deadline_seconds is not None
            else None
        )

        exit_code_val: int | None = None
        while True:
            try:
                exit_code_val = proc.wait(timeout=_RUNNER_HEARTBEAT_INTERVAL)
                break  # process finished
            except subprocess.TimeoutExpired:
                pass

            now = time.time()
            if deadline is not None and now >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                reader.join(timeout=2)
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=124)
                return

            if _shutdown_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                reader.join(timeout=2)
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=1)
                return

            cancel = _report_compute_job(orchestrator_url, token, job_id,
                                          exec_status="running")
            if cancel:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                reader.join(timeout=2)
                _report_compute_job(orchestrator_url, token, job_id,
                                     exec_status="failed", exit_code=1)
                return

        reader.join(timeout=5)

        # Read result.json for metrics (best-effort; tolerate missing/garbled)
        result_metrics: dict | None = None
        try:
            with open(result_file, encoding="utf-8") as fh:
                parsed = json.loads(fh.read())
            if isinstance(parsed, dict):
                filtered = {
                    k: v for k, v in parsed.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                }
                result_metrics = filtered or None
        except (OSError, json.JSONDecodeError, ValueError):
            pass

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
        # Upload durable artifacts (checkpoints/plots) BEFORE tearing down tmpdir.
        # Runs for every terminal path (success, fail, deadline, cancel) so a run
        # that produced output is never discarded.  Strictly best-effort.
        if artifact_dir:
            _upload_artifact(orchestrator_url, token, job_id, artifact_dir)
        with lock:
            active_jobs.discard(job_id)
        _release_capacity(capacity)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _compute_poll_loop(
    orchestrator_url: str,
    token: str,
    active_jobs: set,
    lock: threading.Lock,
    max_concurrent: int,
    capacity: threading.Semaphore | None = None,
    base_poll_interval: float = 2.0,
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Config
    token = _require_env("BLUEPRINT_RUNNER_TOKEN")
    orchestrator_url = _require_env("BLUEPRINT_ORCHESTRATOR_URL").rstrip("/")
    # Resolve the agent binary LAZILY (compute/GPU-only runners must boot without
    # a built agent).  This non-fatal probe is for the startup summary only; the
    # poll loop re-resolves per agent task and fails THAT task (not the process)
    # if the binary is still missing when an agent task arrives.
    agent_binary = _locate_agent_binary()

    max_concurrent = int(os.environ.get("BLUEPRINT_RUNNER_MAX_CONCURRENT", "2"))
    max_concurrent_compute = int(
        os.environ.get("BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS", "2")
    )
    base_poll_interval = float(os.environ.get("BLUEPRINT_RUNNER_POLL_INTERVAL", "2"))

    # Startup summary
    token_prefix = token[:8] + "..." if len(token) >= 8 else token
    print("Blueprint local fleet runner starting", flush=True)
    print(f"  orchestrator : {orchestrator_url}", flush=True)
    print(
        f"  agent binary : {agent_binary or '(not built — compute-only mode; resolved lazily per agent task)'}",
        flush=True,
    )
    print(f"  max concurrent: {max_concurrent}", flush=True)
    print(f"  max compute jobs: {max_concurrent_compute}", flush=True)
    print(f"  runner token : {token_prefix}", flush=True)

    # Detect GPU hardware once at startup
    gpu_capabilities = _detect_gpu()
    if gpu_capabilities.get("has_gpu"):
        print(f"  gpu detected : {gpu_capabilities['gpu_count']}x {gpu_capabilities['gpu_names']}", flush=True)
    else:
        print("  gpu detected : none", flush=True)

    # Initial heartbeat to validate token (includes gpu capabilities)
    try:
        status, body = _post(
            f"{orchestrator_url}/api/runner/heartbeat",
            token,
            {
                "active_task_ids": [],
                "active_compute_job_ids": [],
                "agent_available": agent_binary is not None,
                **gpu_capabilities,
            },
        )
    except Exception as exc:
        print(f"ERROR: initial heartbeat failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if status == 401:
        # NOT fatal: the runner row may simply be inactive after a missed
        # heartbeat (slept/network-dropped host).  Enter the loop anyway — the
        # background heartbeat re-arms the row (status->active) and the poll
        # loops retry with backoff.  (A truly revoked/deleted token will keep
        # 401-ing harmlessly until the operator re-registers.)
        print(
            "[runner] WARNING: initial heartbeat returned 401 — token may be stale or "
            "the runner row inactive; entering poll loop and retrying (heartbeat re-arms it).",
            flush=True,
        )
    elif status not in (200, 204):
        print(f"ERROR: initial heartbeat returned HTTP {status}: {body}", file=sys.stderr)
        sys.exit(1)
    else:
        print("[runner] Token validated. Entering poll loop.", flush=True)

    # Shared state
    active_tasks: dict[str, subprocess.Popen] = {}
    lock = threading.Lock()
    runner_capacity = threading.BoundedSemaphore(max_concurrent)

    active_compute_jobs: set[str] = set()
    compute_lock = threading.Lock()

    # Background threads
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
        ),
        daemon=True,
    )
    heartbeat.start()

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
        daemon=True,
    )
    compute_poll.start()

    poll_interval = base_poll_interval
    next_task_url = f"{orchestrator_url}/api/runner/next-task"

    # Poll loop
    while not _shutdown_event.is_set():
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
            # NOT fatal (see compute poll loop): an inactive runner row 401s
            # until the heartbeat re-arms it.  Back off and retry instead of
            # exiting so a slept/network-dropped host self-heals on reconnect.
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
            # No task available — backoff
            _release_capacity(runner_capacity)
            poll_interval = min(poll_interval * 2, 30.0)
            time.sleep(poll_interval)
            continue

        if status != 200:
            _release_capacity(runner_capacity)
            print(f"[runner] poll warning: HTTP {status}", flush=True)
            time.sleep(5)
            continue

        # Task claimed — reset backoff
        poll_interval = base_poll_interval

        task_id = body.get("task_id") or body.get("id", "unknown")
        agent_token = body.get("agent_token", "")
        task_type = body.get("task_type", "unknown")
        vm_instance_id = body.get("vm_instance_id") or "runner:local"

        # Defense-in-depth: skip if this task is already tracked (e.g. server
        # returned the same task before started_at was visible to this poll).
        with lock:
            already_active = task_id in active_tasks
        if already_active:
            print(f"[runner] skipping duplicate dispatch for task {task_id}", flush=True)
            _release_capacity(runner_capacity)
            time.sleep(poll_interval)
            continue

        # Lazily resolve the agent binary now that an AGENT task is actually here.
        # A compute-only runner gets this far without a built agent; if the binary
        # is missing we fail THIS task gracefully (log + skip) rather than killing
        # the whole process — the compute poll loop keeps running.  Re-resolve each
        # time so a binary built after startup is picked up; cache once found.
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

        # Build subprocess environment
        try:
            workspace = _agent_workspace_for_task(task_id)
        except Exception as exc:
            print(f"[runner] failed to prepare workspace for task {task_id}: {exc}", flush=True)
            _release_capacity(runner_capacity)
            time.sleep(poll_interval)
            continue

        env = os.environ.copy()
        env["AGENT_SECRET_TOKEN"] = agent_token
        env["ORCHESTRATOR_URL"] = orchestrator_url
        env["TASK_ID"] = task_id
        env["VM_INSTANCE_ID"] = vm_instance_id
        env["AGENT_WORKSPACE"] = workspace

        # Spawn agent
        try:
            proc = subprocess.Popen(
                ["node", agent_binary],
                env=env,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        except Exception as exc:
            print(f"[runner] failed to spawn agent for task {task_id}: {exc}", flush=True)
            _release_capacity(runner_capacity)
            continue

        with lock:
            active_tasks[task_id] = proc
        print(
            f"[START] task_id={task_id} type={task_type} vm={vm_instance_id} "
            f"workspace={workspace} pid={proc.pid}",
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
                print(f"[runner] timeout waiting for task {tid} (pid={proc.pid}) — sending SIGTERM", flush=True)
                try:
                    proc.terminate()
                except OSError:
                    pass

    print("[runner] Exiting.", flush=True)


if __name__ == "__main__":
    main()
