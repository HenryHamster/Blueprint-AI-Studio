"""Golden-task agent execution harness.

Runs a single golden-task spec through the real Blueprint agent and captures
real telemetry (token counts, wall time, cache hit rates, cost). Provides a
mock orchestrator HTTP server so the agent can claim, report status, and
heartbeat without a live backend.

Usage (typically called from runner.py, not directly)::

    from golden_tasks.agent_runner import run_single_task, AgentBinaryNotFound

    try:
        metrics = run_single_task(payload, timeout_seconds=600)
    except AgentBinaryNotFound:
        print("Agent binary not found — falling back to simulated metrics")
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentBinaryNotFound(Exception):
    """Raised when the agent binary cannot be located on this machine."""


# ---------------------------------------------------------------------------
# Agent binary resolution (ported from scripts/local_runner.py)
# ---------------------------------------------------------------------------


def _locate_agent_binary() -> list[str] | None:
    """Locate the agent entry argv, or return None if it is unavailable.

    Resolution order (same as ``scripts/local_runner.py``):
      1. ``BLUEPRINT_AGENT_BINARY`` env var (an explicit path — run with node).
      2. ``<golden_tasks>/../agent/dist/index.js`` — compiled dist artifact.
      3. ``<cwd>/agent/dist/index.js`` — same, relative to working directory.
      4. ``<golden_tasks>/../agent/src/index.ts`` via
         ``<agent_dir>/node_modules/.bin/tsx`` — tsx-based fallback.
      5. ``<cwd>/agent/src/index.ts`` via ``tsx`` on PATH — same, cwd-relative.

    Returns the full argv (``["node", "/path/...js"]`` or
    ``["/path/tsx", "/path/...ts"]``).
    """
    explicit = os.environ.get("BLUEPRINT_AGENT_BINARY", "").strip()
    if explicit:
        return ["node", explicit] if os.path.isfile(explicit) else None

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


# ---------------------------------------------------------------------------
# Read-only task capability contract (minimal, satisfies agent's loader)
# ---------------------------------------------------------------------------

_READ_ONLY_TASK_TYPES = frozenset({
    "review", "plan", "research", "strategic_review",
    "design_doc_update", "security_audit", "investigate", "self_audit",
    "release_audit", "release_audit_challenge", "release_merge_grade",
    "memo", "verify_tests", "compute_experiment", "web_research",
    "design", "repo_bootstrap", "perf",
    "capability_proof_trial", "criterion_coverage_trial", "walkthrough_e2e",
})

_ALL_TASK_TYPES = _READ_ONLY_TASK_TYPES | {
    "create", "fix", "refactor", "qa", "deps", "docs", "migrate",
    "smoketest", "debug", "salvage_bundle",
}


def _build_task_capabilities() -> dict[str, dict[str, Any]]:
    """Return a minimal task-capability contract covering all known types."""
    caps: dict[str, dict[str, Any]] = {}
    for t in sorted(_ALL_TASK_TYPES):
        caps[t] = {
            "read_only": t in _READ_ONLY_TASK_TYPES,
            "agent_mode": "default",
            "run_shape": "default",
        }
    return caps


# ---------------------------------------------------------------------------
# Claim response builder
# ---------------------------------------------------------------------------


def _build_claim_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal claim response that satisfies the agent's destructuring.

    The agent extracts ``task_id``, ``title``, ``payloads[0]``,
    ``project_metadata``, and ``title`` from the claim response.
    """
    task_id = str(uuid.uuid4())
    return {
        "task_id": task_id,
        "title": payload.get("title", "Golden task"),
        "payloads": [payload],
        "project_metadata": {
            "project_id": "golden-runner",
            "project_name": "Golden Tasks",
            "github_owner": "HenryHamster",
            "github_repo": "BlueprintAIStudio",
            "build_config": {},
            "integration_branch": "main",
        },
        "context": {},
    }


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

# Pricing in USD per 1M tokens (as of mid-2026).
# These are approximate and used only for golden-task cost comparisons.
_PRICING: dict[str, dict[str, float]] = {
    # model_prefix -> {input_per_1M, output_per_1M, cache_read_per_1M, cache_write_per_1M}
    "claude-sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-haiku": {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00},
}


def _estimate_cost_usd(
    *,
    model_id: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    """Estimate LLM cost from token counts using known provider pricing.

    Returns 0.0 when the model is unrecognised (with a note printed to stderr).
    """
    if not model_id:
        return 0.0

    model_lower = model_id.lower()
    pricing = None
    for prefix, rates in _PRICING.items():
        if model_lower.startswith(prefix):
            pricing = rates
            break

    if pricing is None:
        print(
            f"  [agent_runner] Unrecognised model {model_id!r} — "
            f"cost set to 0.0 (known prefixes: {', '.join(sorted(_PRICING))})",
            file=sys.stderr,
        )
        return 0.0

    # Cache write tokens are already included in input_tokens (the provider
    # reports them as a subset). We charge the premium over the base input rate.
    uncached_input = input_tokens - cache_read_tokens - cache_write_tokens
    if uncached_input < 0:
        uncached_input = 0

    cost = (
        uncached_input / 1_000_000 * pricing["input"]
        + output_tokens / 1_000_000 * pricing["output"]
        + cache_read_tokens / 1_000_000 * pricing["cache_read"]
        + cache_write_tokens / 1_000_000 * pricing["cache_write"]
    )
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Minimal WebSocket frame helpers
# ---------------------------------------------------------------------------

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Frame opcodes
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def _ws_accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()


def _ws_read_frame(sock: socket.socket, timeout: float = 5.0) -> tuple[int, bytes]:
    """Read a single WebSocket frame. Returns (opcode, payload)."""
    sock.settimeout(timeout)

    # Read first 2 bytes
    header = sock.recv(2)
    if len(header) < 2:
        return _OP_CLOSE, b""

    opcode = header[0] & 0x0F
    masked = (header[1] & 0x80) != 0
    length = header[1] & 0x7F

    if length == 126:
        raw = sock.recv(2)
        if len(raw) < 2:
            return _OP_CLOSE, b""
        length = struct.unpack("!H", raw)[0]
    elif length == 127:
        raw = sock.recv(8)
        if len(raw) < 8:
            return _OP_CLOSE, b""
        length = struct.unpack("!Q", raw)[0]

    mask_key = b""
    if masked:
        mask_key = sock.recv(4)
        if len(mask_key) < 4:
            return _OP_CLOSE, b""

    payload = bytearray()
    remaining = length
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            break
        payload.extend(chunk)
        remaining -= len(chunk)

    if masked and mask_key:
        for i in range(len(payload)):
            payload[i] ^= mask_key[i % 4]

    return opcode, bytes(payload)


def _ws_send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Send a single WebSocket frame (server → client, unmasked)."""
    frame = bytearray()
    frame.append(0x80 | opcode)  # FIN + opcode

    length = len(payload)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(struct.pack("!H", length))
    else:
        frame.append(127)
        frame.extend(struct.pack("!Q", length))

    frame.extend(payload)
    sock.sendall(bytes(frame))


# ---------------------------------------------------------------------------
# Mock orchestrator HTTP + WS server
# ---------------------------------------------------------------------------


class _MockOrchestratorHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the mock orchestrator.

    Implements the subset of the agent API needed for a single-task run.
    Also handles WebSocket upgrades for the log stream.
    """

    # Set by the server before starting
    orchestrator: _MockOrchestrator | None = None  # type: ignore[assignment]

    def do_GET(self) -> None:
        orch = self.orchestrator
        if orch is None:
            self._send_json(500, {"error": "server not initialised"})
            return

        if self.path.startswith("/api/agent/github/token"):
            # Return an empty token so the credential helper outputs nothing and
            # git falls through to anonymous access (works for public repos).
            # The real owner/repo are used so the remote URL is correct.
            self._send_json(200, {
                "data": {
                    "token": "",
                    "owner": orch.github_owner,
                    "repo": orch.github_repo,
                },
            })
            return

        if self.path == "/api/agent/task-capabilities":
            self._send_json(200, {"task_capabilities": _build_task_capabilities()})
            return

        if self.path.startswith("/api/agent/ws/"):
            self._handle_ws_upgrade()
            return

        self._send_json(404, {"error": f"not found: {self.path}"})

    def do_POST(self) -> None:
        orch = self.orchestrator
        if orch is None:
            self._send_json(500, {"error": "server not initialised"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            data = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            data = {}

        if self.path == "/api/agent/claim":
            self._send_json(200, _build_claim_response(orch.payload))
            return

        if self.path == "/api/agent/status":
            orch._captured_status = data  # noqa: SLF001
            orch._status_received.set()  # noqa: SLF001
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/api/agent/heartbeat":
            # Heartbeats carry a telemetry snapshot — capture latest for
            # observability but always respond 200 so the agent keeps running.
            orch._last_heartbeat = data  # noqa: SLF001
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/api/agent/vm-heartbeat":
            self._send_json(200, {"status": "ok"})
            return

        self._send_json(404, {"error": f"not found: {self.path}"})

    def _send_json(self, code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_ws_upgrade(self) -> None:
        """Accept a WebSocket upgrade, read the auth token, then idle.

        The agent sends a JSON auth token as the first text frame, then keeps
        the socket open for log streaming. We read the first frame and then
        keep the connection alive (the agent will close it on task completion).
        A failure here is non-fatal — the agent handles missing log streams.
        """
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self._send_json(400, {"error": "missing Sec-WebSocket-Key"})
            return

        accept = _ws_accept_key(key)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        # Read the initial auth frame, then loop reading frames until the
        # agent closes the connection. This prevents the agent's WebSocket
        # connect timeout from adding overhead.
        try:
            sock = self.request
            # Read initial auth frame
            _ws_read_frame(sock, timeout=10.0)
            # Idle loop — read and ignore frames until close
            while True:
                opcode, _payload = _ws_read_frame(sock, timeout=30.0)
                if opcode == _OP_CLOSE:
                    _ws_send_frame(sock, _OP_CLOSE, b"")
                    break
                if opcode == _OP_PING:
                    _ws_send_frame(sock, _OP_PONG, b"")
        except (OSError, ConnectionError, TimeoutError):
            pass  # Agent closed connection or network issue — normal

    def log_message(self, fmt: str, *args: Any) -> None:
        """Suppress http.server's default stderr logging."""
        pass


class _MockOrchestrator:
    """Lightweight threaded HTTP server implementing the agent API subset.

    Starts on a random available port bound to 127.0.0.1.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self._captured_status: dict[str, Any] | None = None
        self._last_heartbeat: dict[str, Any] | None = None
        self._status_received = threading.Event()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0
        # GitHub owner/repo for the workspace remote URL.
        # Match the real repo so anonymous git access works for public repos.
        self.github_owner: str = "HenryHamster"
        self.github_repo: str = "BlueprintAIStudio"

    def start(self) -> None:
        """Bind and start serving in a daemon thread."""
        self._server = HTTPServer(("127.0.0.1", 0), _MockOrchestratorHandler)
        self.port = self._server.server_address[1]
        # Inject the orchestrator reference into the handler class
        _MockOrchestratorHandler.orchestrator = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut down the server."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    @property
    def captured_status(self) -> dict[str, Any] | None:
        return self._captured_status

    @property
    def last_heartbeat(self) -> dict[str, Any] | None:
        return self._last_heartbeat

    def wait_for_status(self, timeout_s: float) -> bool:
        """Block until the agent POSTs a terminal status, or timeout."""
        return self._status_received.wait(timeout=timeout_s)


# ---------------------------------------------------------------------------
# Agent subprocess management
# ---------------------------------------------------------------------------


def _check_llm_credentials() -> list[str]:
    """Check which common LLM credential env vars are set. Returns missing list."""
    missing = []
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        if not os.environ.get(var, "").strip():
            missing.append(var)
    return missing


def _has_required_llm_credentials(provider: str | None = None) -> bool:
    """Return whether the selected provider has credentials for a real run.

    Claude Code treats its OAuth token as the primary Anthropic credential;
    an API key is only a fallback.
    """
    provider = (provider if provider is not None else os.environ.get("PROVIDER", "")).lower()
    if provider in ("anthropic", ""):
        return bool(
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        )
    return True


def run_single_task(
    payload: dict[str, Any],
    *,
    timeout_seconds: int = 600,
    env_override: dict[str, str] | None = None,
    agent_path: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run a single golden-task spec through the real agent.

    Args:
        payload: Task payload dict (from ``GoldenSpec.to_task_payload()``).
        timeout_seconds: Maximum wall-clock time for the agent subprocess.
        env_override: Extra env vars to pass to the agent subprocess.
        agent_path: Override path to agent binary (sets BLUEPRINT_AGENT_BINARY).
        verbose: If True, stream agent stdout/stderr to the runner's stdout.

    Returns:
        A dict with keys matching ``RunMetrics`` fields:
        ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
        ``cost_usd``, ``llm_provider``, ``model_id``, ``wall_time_s``,
        ``success``, ``error``, ``cache_read_input_tokens``,
        ``cache_creation_input_tokens``, ``llm_call_count``, ``num_turns``.

    Raises:
        AgentBinaryNotFound: If the agent binary cannot be located.
    """
    # 1. Locate agent binary
    if agent_path:
        os.environ["BLUEPRINT_AGENT_BINARY"] = agent_path
    binary_argv = _locate_agent_binary()
    if binary_argv is None:
        msg = (
            "Agent binary not found. "
            "Run `cd agent && npm install` to install dependencies, "
            "or set BLUEPRINT_AGENT_BINARY to the entry point path."
        )
        raise AgentBinaryNotFound(msg)

    # 2. Check LLM credentials
    missing_creds = _check_llm_credentials()
    if missing_creds:
        # Not all are required — e.g. if ANTHROPIC_API_KEY is set but
        # OPENAI_API_KEY is not, that's fine for Anthropic runs.
        # Only fail if ALL common credential vars are missing.
        if not _has_required_llm_credentials():
            msg = (
                "No LLM credentials found in environment. "
                "Set ANTHROPIC_API_KEY (or OPENAI_API_KEY, CLAUDE_CODE_OAUTH_TOKEN) "
                "to enable real agent execution."
            )
            raise AgentBinaryNotFound(msg)  # Reuse same type for graceful fallback

    # 3. Start mock orchestrator
    orchestrator = _MockOrchestrator(payload)
    orchestrator.start()

    # 4. Create temp workspace and pre-clone the repo.
    #    We use a local clone so the agent's warm-handoff path can reuse it
    #    without needing real GitHub credentials (empty token → anonymous fallback
    #    for the public repo).
    workspace = tempfile.mkdtemp(prefix="golden_task_")
    log_file_path = os.path.join(workspace, "agent.log")

    _repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _clone_result = subprocess.run(
        ["git", "clone", "--local", _repo_root, workspace],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if _clone_result.returncode != 0:
        # If local clone fails (e.g. not in a git repo), init a bare repo
        # so the agent's .git existence check passes. The cold-path clone
        # will likely fail without real credentials, but the agent will
        # report the failure via status POST.
        subprocess.run(
            ["git", "init", workspace],
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "-C", workspace, "commit", "--allow-empty", "-m", "golden-task-init"],
            capture_output=True,
            timeout=30,
        )

    # 5. Build environment for agent subprocess.
    #    BLUEPRINT_WARM_HANDOFF=1 tells the agent to reuse the existing workspace
    #    (warm path) instead of wiping + re-cloning (cold path). The warm path
    #    configures a credential helper but returns an empty token (public repo
    #    → anonymous fallback).
    task_id = str(uuid.uuid4())
    agent_env = os.environ.copy()
    agent_env.update({
        "ORCHESTRATOR_URL": f"http://127.0.0.1:{orchestrator.port}",
        "AGENT_SECRET_TOKEN": "golden-runner-token",
        "TASK_ID": task_id,
        "VM_INSTANCE_ID": "golden-runner",
        "AGENT_WORKSPACE": workspace,
        # Disable pool mode and job mode
        "BLUEPRINT_POOL_MODE": "0",
        # Warm-handoff: reuse the pre-cloned workspace
        "BLUEPRINT_WARM_HANDOFF": "1",
    })
    # Clear any pre-existing reexec flags
    agent_env.pop("BLUEPRINT_REEXEC", None)

    if env_override:
        agent_env.update(env_override)

    # 6. Spawn agent subprocess
    start_time = time.monotonic()
    log_file: Any = open(log_file_path, "w")  # noqa: SIM115

    try:
        proc = subprocess.Popen(
            binary_argv,
            env=agent_env,
            stdout=subprocess.PIPE if not verbose else None,
            stderr=subprocess.PIPE if not verbose else None,
            start_new_session=True,
            cwd=workspace,
        )
    except OSError as exc:
        log_file.close()
        orchestrator.stop()
        raise AgentBinaryNotFound(
            f"Failed to spawn agent process ({binary_argv!r}): {exc}"
        ) from exc

    # 7. Poll for terminal status or process exit (whichever comes first).
    #    Use short poll intervals so a crashed agent is detected quickly.
    terminal_timeout = min(timeout_seconds, 600)  # Cap at 10 min for golden tasks
    poll_interval = 1.0  # Check process + status every second
    elapsed = 0.0
    status_received = False

    while elapsed < terminal_timeout:
        # Check if the agent process has exited
        exit_code = proc.poll()
        if exit_code is not None:
            # Agent exited — give the orchestrator a moment to process any
            # in-flight status POST that landed just before exit.
            if not status_received:
                status_received = orchestrator.wait_for_status(2.0)
            break

        # Check if a terminal status was posted
        status_received = orchestrator.wait_for_status(poll_interval)
        if status_received:
            # Status received — give the agent a moment to exit gracefully,
            # then collect any remaining output.
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            break

        elapsed += poll_interval

    # If we timed out, kill the agent
    if not status_received and proc.poll() is None:
        _kill_process_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # 8. Collect stdout/stderr
    if proc.stdout is not None:
        stdout_data, stderr_data = proc.communicate(timeout=10)
    else:
        stdout_data, stderr_data = b"", b""
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    if verbose and stdout_data:
        print(stdout_data.decode("utf-8", errors="replace"), file=sys.stderr)
    if not verbose and stderr_data:
        log_file.write(stderr_data.decode("utf-8", errors="replace"))
    log_file.close()

    end_time = time.monotonic()

    # 9. Stop mock orchestrator
    orchestrator.stop()

    # 10. Parse captured status into metrics dict
    status = orchestrator.captured_status
    return _extract_metrics(
        status=status,
        wall_time_s=end_time - start_time,
        proc=proc,
        workspace=workspace,
    )


def _kill_process_group(proc: subprocess.Popen[Any]) -> None:
    """Kill the process group for *proc*."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    # Follow up with SIGKILL after a brief wait
    time.sleep(1)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _extract_metrics(
    *,
    status: dict[str, Any] | None,
    wall_time_s: float,
    proc: subprocess.Popen[Any],
    workspace: str,
) -> dict[str, Any]:
    """Extract RunMetrics-compatible fields from captured agent status.

    Returns a dict ready for RunMetrics population.
    """
    metrics: dict[str, Any] = {
        "wall_time_s": round(wall_time_s, 3),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "success": False,
        "error": None,
        "llm_provider": None,
        "model_id": None,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "llm_call_count": 0,
        "num_turns": 0,
    }

    if status is None:
        # Agent never posted a terminal status
        exit_code = proc.returncode
        if exit_code is None:
            metrics["error"] = "agent timed out without posting status"
        elif exit_code != 0:
            metrics["error"] = f"agent exited with code {exit_code} without posting status"
            # Try to read log file for error details
            log_path = os.path.join(workspace, "agent.log")
            try:
                with open(log_path, "r") as f:
                    log_tail = f.read()[-2000:]
                if log_tail:
                    metrics["error"] += f" — log tail: {log_tail[-500:]}"
            except OSError:
                pass
        else:
            metrics["error"] = "agent exited 0 but never posted terminal status"
        return metrics

    # Terminal status received
    agent_status = str(status.get("status", "")).upper()
    metrics["success"] = agent_status == "COMPLETED"
    if not metrics["success"]:
        failure_cat = status.get("failure_category", "")
        error_report = status.get("error_report", "")
        logs_val = str(status.get("logs", ""))
        metrics["error"] = (
            f"status={agent_status}"
            + (f" failure_category={failure_cat}" if failure_cat else "")
            + (f" error_report={str(error_report)[:200]}" if error_report else "")
            + (f" logs={logs_val[:200]}" if logs_val else "")
        )

    # Token telemetry
    total_input = _safe_int(status.get("total_input_tokens", 0))
    total_output = _safe_int(status.get("total_output_tokens", 0))
    cache_read = _safe_int(status.get("total_cache_read_input_tokens", 0))
    cache_create = _safe_int(status.get("total_cache_creation_input_tokens", 0))

    metrics["prompt_tokens"] = total_input
    metrics["completion_tokens"] = total_output
    metrics["total_tokens"] = total_input + total_output
    metrics["cache_read_input_tokens"] = cache_read
    metrics["cache_creation_input_tokens"] = cache_create

    # Provider / model
    metrics["llm_provider"] = str(status.get("provider", "") or "") or None
    metrics["model_id"] = str(status.get("model", "") or "") or None

    # Call count / turns
    metrics["llm_call_count"] = _safe_int(status.get("llm_call_count", 0))
    metrics["num_turns"] = _safe_int(status.get("num_turns", 0))

    # Cost estimation
    metrics["cost_usd"] = _estimate_cost_usd(
        model_id=metrics["model_id"],
        input_tokens=total_input,
        output_tokens=total_output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_create,
    )

    return metrics


def _safe_int(value: Any) -> int:
    """Coerce *value* to int, returning 0 on any failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
