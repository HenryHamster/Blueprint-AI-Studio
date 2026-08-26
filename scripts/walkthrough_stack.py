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

Standalone ``--run-harness`` frontend-source caveat
---------------------------------------------------
The standalone ``--run-harness`` CLI (a debug/pre-flight tool, not the
qualifier path) serves the LOCAL repo checkout's admin-ui via ``npm run dev``
and runs THIS repo's seed/harness scripts against the CANDIDATE backend image.
A green run proves the candidate backend interoperates with the serving
engine's frontend; it does NOT exercise the candidate's own frontend build.
The qualifier's agent-side boot path (``agent/src/handlers/walkthrough-stack.ts``)
serves the candidate admin-ui from the candidate checkout instead. The emitted
result carries a ``frontend_source`` marker so a green harness run is never
misread as proof the candidate frontend works.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import typing
import urllib.error
import urllib.request

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


def sandbox_proto_dir(sandbox_workspace: str) -> str:
    """The protocol dir as the AGENT sees it inside the sandbox.

    The bwrap workspace bind maps the host workspace to ``sandbox_workspace``
    (e.g. ``/workspace``) and the agent's own WORKSPACE is a SUBDIRECTORY of
    that mount (``/workspace/agent``), so the marker sits one level ABOVE the
    agent's workspace, at the bind root. This function is the single shared
    contract: the runner writes marker files at ``_proto_dir(workspace)`` (host
    side) and passes ``sandbox_proto_dir(sandbox_workspace)`` to the agent so
    its probe lands on the same path — never a second, independently-joined
    string on the far side of the sandbox boundary.
    """
    return os.path.join(sandbox_workspace, PROTOCOL_DIR)


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


def pick_free_ports(count: int) -> list[int]:
    """Return ``count`` distinct free loopback TCP ports.

    Holds every probe socket open until ALL ``count`` ports are allocated so the
    OS cannot hand the same ephemeral port to two back-to-back probes (the old
    single-port helper could return the SAME port for backend and db, so the
    second ``docker run`` publish would collide). Sockets are closed before the
    caller publishes the ports to docker — the small TOCTOU window after release
    is fine: a publish failure fails loudly and surfaces the boot error.
    """
    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(int(sock.getsockname()[1]))
    finally:
        for sock in sockets:
            sock.close()
    return ports


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
        build_context: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.task_id = task_id
        self._run = run or _run_docker
        self._log = log
        # The runner bind layout nests the candidate checkout at
        # <workspace>/agent (see _boot); a standalone CLI may point at a bare
        # checkout where backend/ sits at the root — that caller passes an
        # explicit build_context.
        self.build_context = build_context or os.path.join(
            self.workspace, "agent", "backend"
        )
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

    def _boot(
        self,
        req: dict[str, str],
        *,
        backend_port: int | None = None,
        db_port: int | None = None,
    ) -> tuple[int, int]:
        """Fixed-shape stack boot. Raises on any failure (agent fails closed).

        ``backend_port``/``db_port`` let a caller pin the published loopback
        ports (the standalone CLI's ``--backend-port``/``--db-port``); when
        either is None the port is auto-assigned.
        """
        self._booted = True  # set BEFORE the first docker call so teardown covers partial boots
        if backend_port is None or db_port is None:
            picked = pick_free_ports(2)
            if backend_port is None:
                backend_port = picked[0]
            if db_port is None:
                db_port = picked[1]
        database_url = (
            f"postgresql+asyncpg://postgres:{req['pg_password']}@db:5432/postgres"
        )
        # Ephemeral Fernet key for the throwaway stack. Migration 0609 refuses
        # to stamp without GITHUB_PAT_ENCRYPTION_KEY, and the app would run
        # keyless (plaintext-credential class) without one. The stack's DB is
        # fresh and destroyed at teardown, so a per-boot random key is the
        # correct scope: nothing encrypted here ever needs decrypting by
        # anything outside this stack, and the key never leaves this process.
        stack_encryption_key = base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode("ascii")

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
        # The workspace passed by the runner is the per-task bind ROOT (the
        # marker lives there); the candidate checkout lives at
        # <workspace>/agent, so the backend build context is the agent
        # subdirectory — never the bind root itself. A standalone CLI may
        # pass an explicit build_context (bare-checkout layout).
        self._run(
            ["docker", "build", "-t", self.image, self.build_context],
            timeout=_BUILD_TIMEOUT_S,
        )
        self._wait_for_db()
        self._run(
            [
                "docker", "run", "--rm",
                "--network", self.network,
                "-e", f"DATABASE_URL={database_url}",
                "-e", f"GITHUB_PAT_ENCRYPTION_KEY={stack_encryption_key}",
                self.image,
                "alembic", "upgrade", "head",
            ],
            timeout=_MIGRATE_TIMEOUT_S,
        )
        app_env = {
            "DATABASE_URL": database_url,
            "GITHUB_PAT_ENCRYPTION_KEY": stack_encryption_key,
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


# ── standalone CLI (pre-flight / debug outside the qualifier) ─────────────────
#
# The qualifier path boots a candidate stack only through the full
# runner+orchestrator pipeline (scripts/local_runner.py). This CLI reuses the
# SAME fixed-shape boot (`WalkthroughStackSupervisor._boot`) so a candidate SHA
# can be proven bootable before it gates a promotion — the candidate's own
# docker-compose.yml is never executed.

_CLI_DEFAULT_TIMEOUT_S = 600
_WALKTHROUGH_PROJECT_ID = "00000000-0000-4000-8000-000000000001"
_HARNESS_SCRIPT = "scripts/walkthrough/v26-live-walkthrough.cjs"
_HARNESS_BIN = "node"
_SEED_SCRIPT = "scripts/walkthrough/seed_test_data.py"
_ADMIN_UI_DIR = "admin-ui"
_FRONTEND_DEFAULT_PORT = 3001
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _detect_build_context(workspace: str) -> str:
    """Locate the backend docker build context for a given workspace.

    The runner bind layout nests the candidate checkout at ``<workspace>/agent``
    (backend at ``<workspace>/agent/backend``); a bare checkout puts ``backend/``
    at the root. ``--build-context`` overrides this entirely. When neither shape
    exists the nested candidate is returned so the caller reports a clear error.
    """
    nested = os.path.join(workspace, "agent", "backend")
    if os.path.isdir(nested):
        return nested
    bare = os.path.join(workspace, "backend")
    if os.path.isdir(bare):
        return bare
    return nested


def _cli_build_request(args: argparse.Namespace) -> dict[str, str]:
    """Assemble a validated stack request from CLI flags, minting secrets."""
    def _secret(prefix: str) -> str:
        return f"{prefix}{secrets.token_hex(16)}"

    raw: dict[str, str] = {
        "candidate_sha": args.candidate_sha,
        "pg_password": args.pg_password or _secret("wt-pg-"),
        "agent_jwt_secret": args.agent_jwt_secret or _secret("wt-jwt-"),
        "ws_ticket_secret": args.ws_ticket_secret or _secret("wt-ws-"),
        "admin_token": args.admin_token or _secret("wt-admin-"),
    }
    if args.anthropic_api_key:
        raw["anthropic_api_key"] = args.anthropic_api_key
    clean, err = validate_request(raw)
    if clean is None:
        raise ValueError(err)
    return clean


def _http_get_json(
    url: str, *, timeout: float, headers: dict[str, str] | None = None
) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _wait_until_healthy(backend_port: int, admin_token: str, *, deadline: float) -> None:
    """HTTP-poll ``/health`` until 200 or the boot deadline elapses."""
    last = "no attempt"
    while time.monotonic() < deadline:
        try:
            status, _ = _http_get_json(
                f"http://127.0.0.1:{backend_port}/health",
                timeout=5,
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            if status == 200:
                return
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            last = str(exc)[:200]
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"candidate backend never became healthy (last: {last})")


def _fetch_engine_sha(backend_port: int, *, timeout: float) -> str:
    """Read the engine SHA from ``/api/version`` (the evidence-integrity seam)."""
    _, body = _http_get_json(
        f"http://127.0.0.1:{backend_port}/api/version", timeout=timeout
    )
    return str(body.get("sha") or body.get("revision") or "unknown")


def _cli_check(args: argparse.Namespace) -> int:
    """Pre-flight: docker reachable + a candidate checkout with a backend."""
    workspace = args.workspace or os.getcwd()
    build_context = args.build_context or _detect_build_context(workspace)
    problems: list[str] = []
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            encoding="utf-8",
            timeout=_DOCKER_SMALL_TIMEOUT_S,
        )
        if result.returncode != 0:
            problems.append(f"docker daemon unreachable (exit {result.returncode})")
    except Exception as exc:  # noqa: BLE001 — pre-flight surface, never raises
        problems.append(f"docker daemon unreachable: {exc}")
    if not os.path.isdir(build_context):
        problems.append(f"no backend/ build context at {build_context}")
    try:
        # ``-c safe.directory=<workspace>`` is load-bearing. ``--workspace`` is
        # an arbitrary operator-supplied path, so the checkout is routinely
        # owned by a uid other than the one running this pre-flight (a
        # runner-created workspace inspected by an operator, a checkout bound
        # into a container that runs as root). Without it git answers every
        # command with "detected dubious ownership" (exit 128) and this branch
        # reports "is not a git checkout" about a perfectly good checkout --
        # a fail-closed refusal built on a read that never happened. The
        # refusal below stays exactly as-is; only the read is repaired.
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={workspace}",
                "-C",
                workspace,
                "rev-parse",
                "--is-inside-work-tree",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=_DOCKER_SMALL_TIMEOUT_S,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            problems.append(f"{workspace} is not a git checkout")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"git checkout check failed: {exc}")
    for problem in problems:
        print(f"[walkthrough-stack-cli] prerequisite check failed: {problem}")
    return 0 if not problems else 1


def _serve_frontend(frontend_port: int) -> subprocess.Popen:
    """Spawn the admin-ui dev server; vite is pinned via --strictPort so the
    harness URL can never silently point at a different port."""
    return subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(frontend_port), "--strictPort"],
        cwd=os.path.join(_REPO_ROOT, _ADMIN_UI_DIR),
        env=dict(os.environ),
    )


def _run_harness(
    req: dict[str, str],
    *,
    backend_port: int,
    db_port: int,
    frontend_port: int,
    workspace: str,
    frontend_holder: list,
) -> None:
    """Seed fixture data, serve the admin-ui, run the node walkthrough harness.

    The seed and harness scripts ship in this repo and run against the stack's
    published loopback ports. ``frontend_holder`` receives the dev-server Popen
    so teardown can kill it on any exit path. Prints the harness result JSON.

    CAVEAT (frontend source): the served admin-ui is THIS local repo checkout's
    vite dev server and the seed/harness scripts come from this checkout too —
    only the backend is the candidate image. A green result therefore proves
    the candidate backend against the serving engine's frontend, NOT the
    candidate's own frontend build (the qualifier's agent-side boot path
    exercises that). The printed result is annotated with a ``frontend_source``
    marker so a green run is never misread as candidate-frontend proof.
    """
    seed_env = dict(os.environ)
    seed_env["DATABASE_URL"] = (
        f"postgresql+asyncpg://postgres:{req['pg_password']}"
        f"@127.0.0.1:{db_port}/postgres"
    )
    subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, _SEED_SCRIPT)],
        env=seed_env,
        timeout=_MIGRATE_TIMEOUT_S,
        check=True,
    )
    frontend = _serve_frontend(frontend_port)
    frontend_holder.append(frontend)
    result_file = os.path.join(workspace, "walkthrough-result.json")
    harness_env = dict(os.environ)
    harness_env.update(
        {
            "WALKTHROUGH_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
            # vite runs with --host=localhost (admin-ui package.json), so the
            # harness must hit the frontend via localhost, not 127.0.0.1.
            "WALKTHROUGH_FRONTEND_URL": f"http://localhost:{frontend_port}",
            "WALKTHROUGH_ADMIN_TOKEN": req["admin_token"],
            "WALKTHROUGH_PROJECT_ID": _WALKTHROUGH_PROJECT_ID,
            "GIT_SHA": req["candidate_sha"],
            "WALKTHROUGH_RESULT_FILE": result_file,
        }
    )
    subprocess.run(
        [_HARNESS_BIN, os.path.join(_REPO_ROOT, _HARNESS_SCRIPT)],
        env=harness_env,
        timeout=_MIGRATE_TIMEOUT_S,
        check=True,
    )
    with open(result_file, encoding="utf-8") as fh:
        result = json.load(fh)
    if isinstance(result, dict):
        # Annotate the machine-readable result with the frontend source so a
        # green run is never misread as proof the candidate's own frontend
        # works (the admin-ui and seed/harness scripts come from the local
        # checkout).
        result["frontend_source"] = "local_repo_checkout"
        result["frontend_caveat"] = (
            "UI steps served the LOCAL repo checkout admin-ui (vite dev server) plus "
            "this repo's seed/harness scripts against the CANDIDATE backend; the "
            "candidate's own frontend build was NOT exercised by this run."
        )
    print(json.dumps(result, sort_keys=True))


def _handle_signal(signum, frame):  # noqa: ARG001 — frame required by signal API
    """Signal handler: SystemExit lets the registered atexit cleanup run."""
    sys.exit(128 + signum)


def _setup_teardown(
    sup: "WalkthroughStackSupervisor", frontend_holder: list, *, keep: bool
) -> typing.Callable[[], None]:
    """Register atexit + SIGINT/SIGTERM teardown; return the cleanup callable.

    ``keep`` skips every registration so an operator can inspect a live stack.
    The returned cleanup is idempotent (``teardown`` and frontend kill both are).
    """
    def cleanup() -> None:
        if frontend_holder:
            frontend = frontend_holder[0]
            frontend.terminate()
            try:
                frontend.wait(timeout=10)
            except Exception:  # noqa: BLE001 — kill as a last resort
                try:
                    frontend.kill()
                except Exception:  # noqa: BLE001
                    pass
        sup.teardown()

    if not keep:
        atexit.register(cleanup)
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, _handle_signal)
            except ValueError:
                pass  # only the main thread can install handlers
    return cleanup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="walkthrough_stack",
        description=(
            "Boot the fixed-shape walkthrough_e2e candidate stack standalone, "
            "decoupled from the qualifier pipeline. The candidate's own "
            "docker-compose.yml is NEVER executed."
        ),
    )
    parser.add_argument("--candidate-sha", metavar="SHA", help="candidate engine SHA to boot")
    parser.add_argument(
        "--workspace",
        metavar="DIR",
        help="candidate checkout (default: a temp dir for boot, cwd for --check)",
    )
    parser.add_argument(
        "--build-context", metavar="DIR", help="override the backend docker build context"
    )
    parser.add_argument("--backend-port", type=int, default=0)
    parser.add_argument("--db-port", type=int, default=0)
    parser.add_argument("--frontend-port", type=int, default=_FRONTEND_DEFAULT_PORT)
    parser.add_argument(
        "--timeout", type=float, default=_CLI_DEFAULT_TIMEOUT_S, help="boot deadline in seconds"
    )
    parser.add_argument("--admin-token")
    parser.add_argument("--anthropic-api-key")
    parser.add_argument("--pg-password")
    parser.add_argument("--agent-jwt-secret")
    parser.add_argument("--ws-ticket-secret")
    parser.add_argument(
        "--run-harness",
        action="store_true",
        help="seed fixture data, serve admin-ui, run the node walkthrough harness",
    )
    parser.add_argument(
        "--check", action="store_true", help="validate prerequisites only (no containers)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="skip teardown (debugging a live stack)"
    )
    return parser


def main(argv: list[str] | None = None, *, run=None) -> int:
    """Standalone entry point; returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check:
        return _cli_check(args)
    if not args.candidate_sha:
        parser.error("--candidate-sha is required (unless --check)")
    try:
        req = _cli_build_request(args)
    except ValueError as exc:
        print(f"[walkthrough-stack-cli] invalid request: {exc}", file=sys.stderr)
        return 2
    workspace = args.workspace or tempfile.mkdtemp(prefix="bp-wt-cli-")
    build_context = args.build_context or _detect_build_context(workspace)
    if not os.path.isdir(build_context):
        print(
            f"[walkthrough-stack-cli] no backend/ build context at {build_context}; "
            "pass --workspace pointing at a candidate checkout or --build-context",
            file=sys.stderr,
        )
        return 2
    sup = WalkthroughStackSupervisor(
        workspace=workspace,
        task_id="standalone-cli",
        run=run,
        build_context=build_context,
        log=lambda message: print(f"[walkthrough-stack-cli] {message}", flush=True),
    )
    frontend_holder: list = []
    cleanup = None
    if not args.keep:
        cleanup = _setup_teardown(sup, frontend_holder, keep=False)
    exit_code = 1
    try:
        backend_port, db_port = sup._boot(
            req,
            backend_port=args.backend_port or None,
            db_port=args.db_port or None,
        )
        print(
            f"[walkthrough-stack-cli] stack booted: backend=127.0.0.1:{backend_port} "
            f"db=127.0.0.1:{db_port}",
            flush=True,
        )
        deadline = time.monotonic() + args.timeout
        _wait_until_healthy(backend_port, req["admin_token"], deadline=deadline)
        sha = _fetch_engine_sha(backend_port, timeout=min(15.0, args.timeout))
        print(f"[walkthrough-stack-cli] candidate engine SHA: {sha}", flush=True)
        if args.run_harness:
            _run_harness(
                req,
                backend_port=backend_port,
                db_port=db_port,
                frontend_port=args.frontend_port,
                workspace=workspace,
                frontend_holder=frontend_holder,
            )
        status = {
            "status": "ok",
            "backend_port": backend_port,
            "db_port": db_port,
            "sha": sha,
        }
        if args.run_harness:
            # The harness served the LOCAL admin-ui against the CANDIDATE backend;
            # label the status so a green run is never misread as proof the
            # candidate's own frontend works (see _run_harness docstring).
            status["frontend_source"] = "local_repo_checkout"
        print(json.dumps(status), flush=True)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 — CLI surfaces the failure and exits non-zero
        print(f"[walkthrough-stack-cli] {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if cleanup is not None:
            cleanup()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
