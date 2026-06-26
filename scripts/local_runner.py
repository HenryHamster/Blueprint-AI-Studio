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
import signal
import subprocess
import sys
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

def _heartbeat_loop(orchestrator_url: str, token: str, active_tasks: dict, lock: threading.Lock) -> None:
    url = f"{orchestrator_url}/api/runner/heartbeat"
    while not _shutdown_event.is_set():
        try:
            with lock:
                task_ids = list(active_tasks.keys())
            status, _ = _post(url, token, {"active_task_ids": task_ids})
            if status not in (200, 204):
                print(f"[runner] heartbeat warning: HTTP {status}", flush=True)
        except Exception as exc:
            print(f"[runner] heartbeat error: {exc}", flush=True)
        # Sleep in 1s increments so shutdown propagates quickly
        for _ in range(30):
            if _shutdown_event.is_set():
                break
            time.sleep(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Config
    token = _require_env("BLUEPRINT_RUNNER_TOKEN")
    orchestrator_url = _require_env("BLUEPRINT_ORCHESTRATOR_URL").rstrip("/")
    agent_binary = _find_agent_binary()

    max_concurrent = int(os.environ.get("BLUEPRINT_RUNNER_MAX_CONCURRENT", "2"))
    base_poll_interval = float(os.environ.get("BLUEPRINT_RUNNER_POLL_INTERVAL", "2"))

    # Startup summary
    token_prefix = token[:8] + "..." if len(token) >= 8 else token
    print("Blueprint local fleet runner starting", flush=True)
    print(f"  orchestrator : {orchestrator_url}", flush=True)
    print(f"  agent binary : {agent_binary}", flush=True)
    print(f"  max concurrent: {max_concurrent}", flush=True)
    print(f"  runner token : {token_prefix}", flush=True)

    # Initial heartbeat to validate token
    try:
        status, body = _post(f"{orchestrator_url}/api/runner/heartbeat", token, {"active_task_ids": []})
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

    # Background threads
    reaper = threading.Thread(target=_reap_loop, args=(active_tasks, lock), daemon=True)
    reaper.start()

    heartbeat = threading.Thread(
        target=_heartbeat_loop, args=(orchestrator_url, token, active_tasks, lock), daemon=True
    )
    heartbeat.start()

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
