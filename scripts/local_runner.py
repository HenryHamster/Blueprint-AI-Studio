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
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


# ── Configuration ────────────────────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: required environment variable {name} is not set.", file=sys.stderr)
        print(f"  Set it and re-run: export {name}=<value>", file=sys.stderr)
        sys.exit(1)
    return value


def _find_agent_binary() -> str:
    explicit = os.environ.get("BLUEPRINT_AGENT_BINARY", "").strip()
    if explicit:
        if not os.path.isfile(explicit):
            print(f"ERROR: BLUEPRINT_AGENT_BINARY={explicit!r} does not exist.", file=sys.stderr)
            sys.exit(1)
        return explicit

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "..", "agent", "dist", "index.js"),
        os.path.join(os.getcwd(), "agent", "dist", "index.js"),
    ]
    for path in candidates:
        normalized = os.path.normpath(path)
        if os.path.isfile(normalized):
            return normalized

    print(
        "ERROR: could not find agent/dist/index.js. "
        "Build the agent first (cd agent && npm run build) "
        "or set BLUEPRINT_AGENT_BINARY to its path.",
        file=sys.stderr,
    )
    sys.exit(1)


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
_RUNNER_MAX_COMMAND_LOG_BYTES = 32768
_RUNNER_HEARTBEAT_INTERVAL = 30.0


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

def _reap_loop(active_tasks: dict, lock: threading.Lock) -> None:
    while not _shutdown_event.is_set():
        with lock:
            done = [tid for tid, proc in active_tasks.items() if proc.poll() is not None]
            for tid in done:
                code = active_tasks[tid].returncode
                del active_tasks[tid]
                print(f"[DONE] task_id={tid} exit_code={code}", flush=True)
        time.sleep(2)


# ── Heartbeat (background thread) ─────────────────────────────────────────────

def _heartbeat_loop(orchestrator_url: str, token: str, active_tasks: dict, lock: threading.Lock, gpu_capabilities: dict) -> None:
    url = f"{orchestrator_url}/api/runner/heartbeat"
    while not _shutdown_event.is_set():
        try:
            with lock:
                task_ids = list(active_tasks.keys())
            body = {"active_task_ids": task_ids, **gpu_capabilities}
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


def _run_compute_job(
    job_body: dict,
    orchestrator_url: str,
    token: str,
    active_jobs: set,
    lock: threading.Lock,
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

        # Capture stdout in a background thread (bounded to _RUNNER_MAX_COMMAND_LOG_BYTES)
        output_state: dict = {"captured": bytearray(), "truncated": False}

        def _read_stdout() -> None:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                if len(output_state["captured"]) < _RUNNER_MAX_COMMAND_LOG_BYTES:
                    remaining = _RUNNER_MAX_COMMAND_LOG_BYTES - len(output_state["captured"])
                    output_state["captured"].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        output_state["truncated"] = True
                else:
                    output_state["truncated"] = True

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

        captured = output_state["captured"]
        truncated = output_state["truncated"]
        command_log: dict | None = None
        if captured or truncated:
            command_log = {
                "text": captured.decode("utf-8", "replace"),
                "truncated": truncated,
                "byte_count": len(captured),
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
        with lock:
            active_jobs.discard(job_id)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _compute_poll_loop(
    orchestrator_url: str,
    token: str,
    active_jobs: set,
    lock: threading.Lock,
    max_concurrent: int,
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

        try:
            status, body = _post(url, token, {})
        except Exception as exc:
            print(f"[compute] poll error: {exc}", flush=True)
            time.sleep(5)
            continue

        if status == 401:
            print(
                "ERROR: Runner token rejected (compute poll) — re-register via admin API: "
                "POST /api/admin/projects/{project_id}/runners/register",
                file=sys.stderr,
            )
            _shutdown_event.set()
            break

        if status == 204 or body is None:
            poll_interval = min(poll_interval * 2, 30.0)
            time.sleep(poll_interval)
            continue

        if status != 200:
            print(f"[compute] poll warning: HTTP {status}", flush=True)
            time.sleep(5)
            continue

        poll_interval = base_poll_interval

        job_id = str(body.get("job_id", "unknown"))
        with lock:
            if job_id in active_jobs:
                print(f"[compute] skipping duplicate job {job_id}", flush=True)
                time.sleep(poll_interval)
                continue
            active_jobs.add(job_id)

        print(f"[compute] claimed job {job_id}", flush=True)
        worker = threading.Thread(
            target=_run_compute_job,
            args=(body, orchestrator_url, token, active_jobs, lock),
            daemon=True,
        )
        worker.start()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Config
    token = _require_env("BLUEPRINT_RUNNER_TOKEN")
    orchestrator_url = _require_env("BLUEPRINT_ORCHESTRATOR_URL").rstrip("/")
    agent_binary = _find_agent_binary()

    max_concurrent = int(os.environ.get("BLUEPRINT_RUNNER_MAX_CONCURRENT", "2"))
    max_concurrent_compute = int(
        os.environ.get("BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS", "2")
    )
    base_poll_interval = float(os.environ.get("BLUEPRINT_RUNNER_POLL_INTERVAL", "2"))

    # Startup summary
    token_prefix = token[:8] + "..." if len(token) >= 8 else token
    print("Blueprint local fleet runner starting", flush=True)
    print(f"  orchestrator : {orchestrator_url}", flush=True)
    print(f"  agent binary : {agent_binary}", flush=True)
    print(f"  max concurrent: {max_concurrent}", flush=True)
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
            {"active_task_ids": [], **gpu_capabilities},
        )
    except Exception as exc:
        print(f"ERROR: initial heartbeat failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if status == 401:
        print(
            "ERROR: runner token rejected (401). Re-register via admin API: "
            "POST /api/admin/projects/{project_id}/runners/register",
            file=sys.stderr,
        )
        sys.exit(1)
    if status not in (200, 204):
        print(f"ERROR: initial heartbeat returned HTTP {status}: {body}", file=sys.stderr)
        sys.exit(1)
    print("[runner] Token validated. Entering poll loop.", flush=True)

    # Shared state
    active_tasks: dict[str, subprocess.Popen] = {}
    lock = threading.Lock()

    active_compute_jobs: set[str] = set()
    compute_lock = threading.Lock()

    # Background threads
    reaper = threading.Thread(target=_reap_loop, args=(active_tasks, lock), daemon=True)
    reaper.start()

    heartbeat = threading.Thread(
        target=_heartbeat_loop, args=(orchestrator_url, token, active_tasks, lock, gpu_capabilities), daemon=True
    )
    heartbeat.start()

    compute_poll = threading.Thread(
        target=_compute_poll_loop,
        args=(orchestrator_url, token, active_compute_jobs, compute_lock, max_concurrent_compute),
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

        try:
            status, body = _post(next_task_url, token, {})
        except Exception as exc:
            print(f"[runner] poll error: {exc}", flush=True)
            time.sleep(5)
            continue

        if status == 401:
            print(
                "ERROR: Runner token rejected — re-register via admin API: "
                "POST /api/admin/projects/{project_id}/runners/register",
                file=sys.stderr,
            )
            _shutdown_event.set()
            break

        if status == 204 or body is None:
            # No task available — backoff
            poll_interval = min(poll_interval * 2, 30.0)
            time.sleep(poll_interval)
            continue

        if status != 200:
            print(f"[runner] poll warning: HTTP {status}", flush=True)
            time.sleep(5)
            continue

        # Task claimed — reset backoff
        poll_interval = base_poll_interval

        task_id = body.get("task_id") or body.get("id", "unknown")
        agent_token = body.get("agent_token", "")
        task_type = body.get("task_type", "unknown")

        # Defense-in-depth: skip if this task is already tracked (e.g. server
        # returned the same task before started_at was visible to this poll).
        with lock:
            already_active = task_id in active_tasks
        if already_active:
            print(f"[runner] skipping duplicate dispatch for task {task_id}", flush=True)
            time.sleep(poll_interval)
            continue

        # Build subprocess environment
        env = os.environ.copy()
        env["AGENT_SECRET_TOKEN"] = agent_token
        env["ORCHESTRATOR_URL"] = orchestrator_url

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
            continue

        with lock:
            active_tasks[task_id] = proc
        print(f"[START] task_id={task_id} type={task_type} pid={proc.pid}", flush=True)

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
