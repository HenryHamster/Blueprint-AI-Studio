"""Runner-side candidate-stack supervisor for ``walkthrough_e2e`` tasks.

The walkthrough evidence lane must boot a throwaway stack (postgres + a
backend image built from the candidate tree) — but the agent runs inside a
bwrap sandbox that deliberately has NO docker socket, and every local runner
keeps it that way. The runner process, by contrast, already has a sanctioned
docker path (it is how container compute jobs execute). This module moves the
stack boot to the runner without ever exposing the socket to candidate code.

File protocol over the per-task workspace bind (visible to both sides):

    <workspace>/.walkthrough-stack/
        supervisor.json   runner → agent, written BEFORE the agent spawns.
                          Its presence tells the agent a runner-side stack
                          supervisor is answering; absence → the agent uses
                          its legacy in-sandbox docker path (VM compatibility).
        request.json      agent → runner. Candidate SHA + throwaway secrets
                          the stack should be configured with.
        response.json     runner → agent. {"status": "ok", backend_port,
                          db_port} or {"status": "error", "detail": ...}.
        teardown.json     agent → runner (optional). Early-teardown signal;
                          the supervisor also always tears down when the agent
                          process exits.

Trust model — the request comes from CANDIDATE code, so nothing in it may
steer WHAT runs, only parameterize the fixed stack shape:

  - the runner never executes the candidate's docker-compose.yml (a compose
    file can request arbitrary host bind-mounts). The stack shape is fixed
    HERE: a pinned ``postgres:15`` and an app image built from the workspace
    ``backend/`` context, joined by a private bridge network, published only
    on 127.0.0.1 loopback ports the runner picks itself;
  - every request field is validated against a strict pattern and used only
    as an environment value or image label — never interpolated into shell
    (all invocations are argv lists, no shell=True anywhere);
  - ``docker build`` of an untrusted Dockerfile and running untrusted code
    inside containers is exactly the exposure compute jobs already accept.

Every failure degrades: a supervisor error writes an error response (the
agent fails closed, as it does today) and never takes the runner down.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import typing

PROTOCOL_DIR = ".walkthrough-stack"
PROTOCOL_VERSION = 1

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
# Optional passthrough values (e.g. an API key the agent already holds):
# printable ASCII, no whitespace, bounded length.
_OPAQUE_RE = re.compile(r"^[\x21-\x7e]{1,512}$")

_DB_IMAGE = "postgres:15"
_NAME_PREFIX = "bp-wt"

_BUILD_TIMEOUT_S = 600
_DB_PULL_TIMEOUT_S = 300
_DB_READY_TIMEOUT_S = 150
_MIGRATE_TIMEOUT_S = 360
_DOCKER_SMALL_TIMEOUT_S = 60
_POLL_INTERVAL_S = 1.0

# One-shot guard for the stale-stack GC (see gc_stale_stacks).
_gc_ran = False
_gc_lock = threading.Lock()


def _proto_dir(workspace: str) -> str:
    return os.path.join(workspace, PROTOCOL_DIR)


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def validate_request(request: dict) -> tuple[dict | None, str]:
    """Validate an agent stack request; return (clean_request, error_detail).

    Only recognized fields survive; anything else in the file is ignored.
    """
    if not isinstance(request, dict):
        return None, "request is not a JSON object"
    candidate_sha = str(request.get("candidate_sha") or "").lower()
    if not _SHA_RE.fullmatch(candidate_sha):
        return None, "candidate_sha must be 7-64 hex chars"
    clean: dict[str, str] = {"candidate_sha": candidate_sha}
    for key in ("pg_password", "agent_jwt_secret", "ws_ticket_secret"):
        value = str(request.get(key) or "")
        if not _SECRET_RE.fullmatch(value):
            return None, f"{key} must match {_SECRET_RE.pattern}"
        clean[key] = value
    # The admin token may be operator-supplied, so only require a bounded
    # opaque value (it is used solely as an env assignment in an argv list).
    admin_token = str(request.get("admin_token") or "")
    if not _OPAQUE_RE.fullmatch(admin_token):
        return None, "admin_token is malformed"
    clean["admin_token"] = admin_token
    anthropic_key = request.get("anthropic_api_key")
    if anthropic_key:
        if not isinstance(anthropic_key, str) or not _OPAQUE_RE.fullmatch(
            anthropic_key
        ):
            return None, "anthropic_api_key is malformed"
        clean["anthropic_api_key"] = anthropic_key
    return clean, ""


def pick_free_port() -> int:
    """A loopback TCP port that is free right now (small TOCTOU window is fine:
    the subsequent docker publish fails loudly and the boot error surfaces)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sanitize_task_fragment(task_id: str) -> str:
    frag = re.sub(r"[^a-z0-9]", "", str(task_id).lower())[:8]
    return frag or "notask"


def gc_stale_stacks(
    run: typing.Callable[..., "subprocess.CompletedProcess[str]"] | None = None,
    log: typing.Callable[[str], None] = print,
) -> None:
    """Best-effort removal of leftover bp-wt-* containers from crashed runs.

    Normal paths tear down in ``finally``; this covers a runner that died
    mid-walkthrough. Runs at most once per runner process.
    """
    global _gc_ran
    with _gc_lock:
        if _gc_ran:
            return
        _gc_ran = True
    runner = run or _run_docker
    try:
        listing = runner(
            [
                "docker", "ps", "-a",
                "--filter", f"name={_NAME_PREFIX}-",
                "--format", "{{.Names}}",
            ],
            timeout=_DOCKER_SMALL_TIMEOUT_S,
        )
        names = [n.strip() for n in (listing.stdout or "").splitlines() if n.strip()]
        if names:
            log(f"[walkthrough-stack] GC removing stale containers: {names}")
            runner(
                ["docker", "rm", "-f", *names],
                timeout=_DOCKER_SMALL_TIMEOUT_S,
                check=False,
            )
    except Exception as exc:  # noqa: BLE001 — GC must never break a spawn
        log(f"[walkthrough-stack] GC skipped: {exc}")


def _run_docker(
    argv: list[str],
    *,
    timeout: float,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> "subprocess.CompletedProcess[str]":
    """argv-only docker invocation with captured output (never shell=True)."""
    result = subprocess.run(  # noqa: S603 — fixed argv, validated inputs
        argv,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env if env is not None else dict(os.environ),
    )
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-800:]
        raise RuntimeError(f"{' '.join(argv[:3])}... exited {result.returncode}: {tail}")
    return result


class WalkthroughStackSupervisor:
    """Boots and tears down one candidate stack for one walkthrough task."""

    def __init__(
        self,
        *,
        workspace: str,
        task_id: str,
        run: typing.Callable[..., "subprocess.CompletedProcess[str]"] | None = None,
        log: typing.Callable[[str], None] = print,
    ) -> None:
        self.workspace = workspace
        self.task_id = task_id
        self._run = run or _run_docker
        self._log = log
        unique = f"{_sanitize_task_fragment(task_id)}-{secrets.token_hex(3)}"
        self.network = f"{_NAME_PREFIX}-{unique}-net"
        self.db_name = f"{_NAME_PREFIX}-{unique}-db"
        self.app_name = f"{_NAME_PREFIX}-{unique}-app"
        self.image = f"{_NAME_PREFIX}-{unique}"
        self._booted = False
        self._answered = False
        self._torn_down = False

    # ── protocol files ──
    def write_marker(self) -> None:
        """Advertise the supervisor to the agent (call BEFORE the agent spawns)."""
        proto = _proto_dir(self.workspace)
        os.makedirs(proto, exist_ok=True)
        _atomic_write_json(
            os.path.join(proto, "supervisor.json"),
            {"version": PROTOCOL_VERSION, "task_id": self.task_id},
        )

    def _respond(self, payload: dict) -> None:
        self._answered = True
        try:
            _atomic_write_json(
                os.path.join(_proto_dir(self.workspace), "response.json"), payload
            )
        except OSError as exc:
            self._log(f"[walkthrough-stack] could not write response: {exc}")

    # ── lifecycle ──
    def run(self, proc: "subprocess.Popen") -> None:
        """Supervise until the agent process exits. Run in a daemon thread."""
        proto = _proto_dir(self.workspace)
        request_path = os.path.join(proto, "request.json")
        teardown_path = os.path.join(proto, "teardown.json")
        try:
            gc_stale_stacks(run=self._run, log=self._log)
            while proc.poll() is None:
                if os.path.exists(teardown_path):
                    break
                if not self._answered and os.path.exists(request_path):
                    self._handle_request(request_path)
                time.sleep(_POLL_INTERVAL_S)
        except Exception as exc:  # noqa: BLE001 — never crash the runner
            self._log(f"[walkthrough-stack] supervisor error for {self.task_id}: {exc}")
            if not self._answered:
                self._respond({"status": "error", "detail": f"supervisor error: {exc}"})
        finally:
            self.teardown()

    def _handle_request(self, request_path: str) -> None:
        raw = _read_json(request_path)
        if raw is None:
            # Possibly a torn write; give the agent one more poll cycle.
            time.sleep(_POLL_INTERVAL_S)
            raw = _read_json(request_path)
            if raw is None:
                self._respond(
                    {"status": "error", "detail": "request.json is unreadable"}
                )
                return
        clean, err = validate_request(raw)
        if clean is None:
            self._respond({"status": "error", "detail": f"invalid request: {err}"})
            return
        self._log(
            f"[walkthrough-stack] booting candidate stack for task {self.task_id} "
            f"@ {clean['candidate_sha'][:12]}"
        )
        try:
            backend_port, db_port = self._boot(clean)
        except Exception as exc:  # noqa: BLE001 — fail the walkthrough, not the runner
            detail = str(exc)[:1000]
            self._log(f"[walkthrough-stack] boot failed for {self.task_id}: {detail}")
            self._respond({"status": "error", "detail": detail})
            return
        self._respond(
            {
                "status": "ok",
                "version": PROTOCOL_VERSION,
                "backend_port": backend_port,
                "db_port": db_port,
            }
        )
        self._log(
            f"[walkthrough-stack] candidate stack ready for {self.task_id}: "
            f"backend=127.0.0.1:{backend_port} db=127.0.0.1:{db_port}"
        )

    def _boot(self, req: dict[str, str]) -> tuple[int, int]:
        """Fixed-shape stack boot. Raises on any failure (agent fails closed)."""
        self._booted = True  # set BEFORE the first docker call so teardown covers partial boots
        backend_port = pick_free_port()
        db_port = pick_free_port()
        database_url = (
            f"postgresql+asyncpg://postgres:{req['pg_password']}@db:5432/postgres"
        )

        self._run(
            ["docker", "network", "create", self.network],
            timeout=_DOCKER_SMALL_TIMEOUT_S,
        )
        self._run(
            [
                "docker", "run", "-d",
                "--name", self.db_name,
                "--network", self.network,
                "--network-alias", "db",
                "-e", "POSTGRES_USER=postgres",
                "-e", f"POSTGRES_PASSWORD={req['pg_password']}",
                "-e", "POSTGRES_DB=postgres",
                "-p", f"127.0.0.1:{db_port}:5432",
                _DB_IMAGE,
            ],
            timeout=_DB_PULL_TIMEOUT_S,
        )
        self._run(
            ["docker", "build", "-t", self.image, os.path.join(self.workspace, "backend")],
            timeout=_BUILD_TIMEOUT_S,
        )
        self._wait_for_db()
        self._run(
            [
                "docker", "run", "--rm",
                "--network", self.network,
                "-e", f"DATABASE_URL={database_url}",
                self.image,
                "alembic", "upgrade", "head",
            ],
            timeout=_MIGRATE_TIMEOUT_S,
        )
        app_env = {
            "DATABASE_URL": database_url,
            "AGENT_JWT_SECRET": req["agent_jwt_secret"],
            "WS_TICKET_SECRET": req["ws_ticket_secret"],
            "GITHUB_APP_ID": "0",
            "GITHUB_PRIVATE_KEY": "",
            "GITHUB_WEBHOOK_SECRET": "walkthrough-local",
            "ORCHESTRATOR_URL": "http://app:8080",
            "ANTHROPIC_API_KEY": req.get("anthropic_api_key", ""),
            "LOCAL_MODE": "true",
            "ADMIN_API_KEY": req["admin_token"],
            "MAX_CONCURRENT_VMS": "5",
            "DEPLOY_SHA": req["candidate_sha"],
        }
        run_argv = [
            "docker", "run", "-d",
            "--name", self.app_name,
            "--network", self.network,
            "--network-alias", "app",
            "-p", f"127.0.0.1:{backend_port}:8080",
        ]
        for key, value in app_env.items():
            run_argv += ["-e", f"{key}={value}"]
        run_argv.append(self.image)
        self._run(run_argv, timeout=_DOCKER_SMALL_TIMEOUT_S)
        return backend_port, db_port

    def _wait_for_db(self) -> None:
        deadline = time.monotonic() + _DB_READY_TIMEOUT_S
        last = "no attempt"
        while time.monotonic() < deadline:
            try:
                self._run(
                    ["docker", "exec", self.db_name, "pg_isready", "-U", "postgres"],
                    timeout=15,
                )
                return
            except Exception as exc:  # noqa: BLE001 — retried until deadline
                last = str(exc)[:200]
                time.sleep(2)
        raise RuntimeError(f"candidate postgres never became ready (last: {last})")

    def teardown(self) -> None:
        """Idempotent, never-throwing removal of every provisioned resource."""
        if self._torn_down:
            return
        self._torn_down = True
        if not self._booted:
            return
        self._log(f"[walkthrough-stack] tearing down candidate stack for {self.task_id}")
        for argv, timeout in (
            (["docker", "rm", "-f", self.app_name, self.db_name], 120),
            (["docker", "network", "rm", self.network], _DOCKER_SMALL_TIMEOUT_S),
            (["docker", "rmi", "-f", self.image], 120),
        ):
            try:
                self._run(argv, timeout=timeout, check=False)
            except Exception as exc:  # noqa: BLE001 — best-effort
                self._log(f"[walkthrough-stack] teardown step {argv[:3]} failed: {exc}")
