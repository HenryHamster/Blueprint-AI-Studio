#!/usr/bin/env python3
"""blueprint-runner — conventional launcher for a Blueprint self-hosted runner.

Wraps ``scripts/local_runner.py`` the way ``buildkite-agent start`` or the GitHub
Actions ``./run.sh`` wrap their runner process: the operator supplies only a
token. The orchestrator URL is **baked in** (production by default; override with
``--url`` or ``BLUEPRINT_ORCHESTRATOR_URL``), so a blank/wrong URL — historically
the most common enrollment failure — is impossible.

    blueprint-runner start --token <token>     # run the runner (blocks)
    blueprint-runner check --token <token>     # preflight only, claim no work

The token may also come from ``BLUEPRINT_RUNNER_TOKEN`` (which, unlike ``--token``,
keeps it off this launcher's own argv / ``/proc/cmdline``).

Prerequisites (Node.js >= 22 plus provider CLIs) are installed by the
operator the normal way. This launcher only *checks* for them and prints an
actionable message when one is missing — it never bundles or installs anything
(standard dev-tool convention). ``local_runner.py`` still enforces its own
fail-closed isolation preflight; this wrapper just makes the common path clean.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import runner_exact_update

# Baked-in production orchestrator base URL. Keep in sync with the admin-UI
# constant DEFAULT_ORCHESTRATOR_URL in admin-ui/src/components/FleetRunnersPanel.utils.ts
# so the enroll dialog only appends --url when the project points elsewhere.
DEFAULT_ORCHESTRATOR_URL = "https://hamsterwheel.tail552e05.ts.net"

# The node agent (agent/dist/index.js) requires a modern Node runtime and spawns
# provider CLIs for agent tasks. Everything else is handled by local_runner's
# own fail-closed preflight.
MIN_NODE_MAJOR = 22
MIN_CODEX_VERSION = (0, 144, 1)

# Canonical first-launch subcommands documented in docs/RUNNER_RUNBOOK.md.
# Keep this tuple in sync with the "Enroll and start" section of the runbook.
# test_docs_runbook_drift.py::test_first_launch_matches_shipped_cli guards the alignment.
FIRST_LAUNCH_COMMANDS: tuple[str, ...] = ("start", "check", "install")
PINNED_CODEX_VERSION = "0.144.1"
_CODEX_REPAIR = (
    f"Reinstall Codex with optional native packages: npm install -g "
    f"--include=optional @openai/codex@{PINNED_CODEX_VERSION}"
)

# Default local tag for the prod agent container image (built from agent/Dockerfile).
# On `start`, when docker is usable, the launcher ensures this image exists (builds
# it if absent) and hands it to local_runner via BLUEPRINT_AGENT_IMAGE, so read-only
# agent tasks run in the prod image (non-root, prod-parity) with ZERO operator setup.
# Overridable with --agent-image or the BLUEPRINT_AGENT_IMAGE env var.
DEFAULT_AGENT_IMAGE_TAG = "blueprint-agent:local"
_AGENT_SOURCE_LABEL = "com.blueprint.agent.source-revision"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_RUNNER = os.path.join(_SCRIPT_DIR, "local_runner.py")
# The prod agent image builds from <repo>/agent (agent/Dockerfile is the default
# Dockerfile in that build context). scripts/ lives directly under the repo root,
# so the agent build context is scripts/../agent.
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")

# systemd unit templates (real, lintable files on disk — rendered, never hand-edited).
SERVICE_UNIT_NAME = "blueprint-runner.service"
_USER_SERVICE_TEMPLATE = os.path.join(_SCRIPT_DIR, "blueprint-runner.service")
_SYSTEM_SERVICE_TEMPLATE = os.path.join(_SCRIPT_DIR, "blueprint-runner-system.service")
_SYSTEM_WANTED_BY = "multi-user.target"
_RUNNER_INSTANCE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")

# The LiveKit gateway is a deliberately narrow companion service for a selected
# always-on Linux host. It is not registered as runner capacity and cannot be
# claimed as a task. The runner CLI owns only its installation/update convention;
# systemd owns the gateway lifetime independently of runner heartbeats.
LIVEKIT_SERVICE_UNIT_NAME = "blueprint-livekit.service"
# Kept outside the root-only secrets directory so an unprivileged runner daemon
# can still observe the reservation even while the media service is stopped.
LIVEKIT_HOST_ROLE_MARKER = "/etc/blueprint-livekit-host-role"
_LIVEKIT_DIR = os.path.join(_REPO_ROOT, "infra", "livekit")
_LIVEKIT_SYSTEM_SERVICE_TEMPLATE = os.path.join(_SCRIPT_DIR, "blueprint-livekit-system.service")

# Resource-limit env vars: (cli-flag, env-var, argparse-dest, help-text).
# Used by the install flags and by `status` to show effective config.
_RESOURCE_LIMIT_VARS: list[tuple[str, str, str, str]] = [
    (
        "--max-concurrent",
        "BLUEPRINT_RUNNER_MAX_CONCURRENT",
        "max_concurrent",
        "Max simultaneous agent subprocesses (default: 2).",
    ),
    (
        "--max-concurrent-compute-jobs",
        "BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS",
        "max_concurrent_compute_jobs",
        "Max simultaneous compute jobs (default: 2).",
    ),
    (
        "--gpu-indices",
        "BLUEPRINT_RUNNER_GPU_INDICES",
        "gpu_indices",
        "Comma-separated GPU-index allowlist (default: all GPUs).",
    ),
    (
        "--gpus-per-job",
        "BLUEPRINT_RUNNER_GPUS_PER_JOB",
        "gpus_per_job",
        "GPUs assigned per compute job (default: 1).",
    ),
    (
        "--cpu-limit",
        "BLUEPRINT_RUNNER_CPU_LIMIT",
        "cpu_limit",
        "Best-effort CPU cap (cores) per task.",
    ),
    (
        "--mem-limit",
        "BLUEPRINT_RUNNER_MEM_LIMIT",
        "mem_limit",
        "Best-effort memory cap per task (e.g. 8g).",
    ),
    (
        "--min-disk-bytes",
        "BLUEPRINT_RUNNER_MIN_DISK_BYTES",
        "min_disk_bytes",
        "Stop claiming work when free disk space is below this many bytes (default: 0 = disabled).",
    ),
]


@dataclass(frozen=True)
class Prereq:
    """One host prerequisite and whether it is satisfied."""

    name: str
    ok: bool
    detail: str
    fix: str | None = None


def _node_major() -> int | None:
    """Return the installed Node.js major version, or None if node is absent/broken."""
    try:
        result = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.match(r"v?(\d+)\.", result.stdout.strip())
    return int(match.group(1)) if match else None


def _check_provider_credentials() -> bool:
    """Return True iff at least one LLM provider credential is set in the environment.

    The agent subprocess needs at least one of these to authenticate with an LLM
    backend.  A runner CAN start without them (compute-only jobs do not need LLM
    access), but ``--check`` should warn the operator so they are not surprised when
    every agent task fails at PRE_CLAIM_FAILED.

    The checked env-variable names are the primary credential keys from
    ``runner_isolation.AGENT_ENV_ALLOWLIST``.
    """
    _CREDENTIAL_KEYS = (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ZAI_API_KEY",
        "OPENAI_API_KEY",
    )
    return any(os.environ.get(k, "").strip() for k in _CREDENTIAL_KEYS)


def _codex_prerequisite() -> Prereq:
    """Check that Codex is on PATH *and* can load its native executable.

    ``codex --version`` is deliberately used as the smoke: it enters the npm
    launcher/native-package startup path, catches a missing
    ``@openai/codex-<platform>`` optional dependency, and never calls an API.
    """
    codex_path = shutil.which("codex")
    if not codex_path:
        return Prereq("codex", False, "not found on PATH", _CODEX_REPAIR)
    try:
        result = subprocess.run(
            [codex_path, "--version"], capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        return Prereq("codex", False, "startup probe timed out", _CODEX_REPAIR)
    except (OSError, subprocess.SubprocessError) as exc:
        return Prereq(
            "codex", False, f"could not execute ({type(exc).__name__})", _CODEX_REPAIR
        )

    output = "\n".join((result.stdout or "", result.stderr or "")).strip()
    if result.returncode != 0:
        lowered = output.lower()
        if (
            "missing optional dependency @openai/codex-" in lowered
            or "could not find package @openai/codex-" in lowered
        ):
            detail = "PATH shim found, but the native Codex package is missing"
        else:
            detail = f"startup probe failed (exit {result.returncode})"
        return Prereq("codex", False, detail, _CODEX_REPAIR)

    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    if not match:
        return Prereq("codex", False, "version probe returned no version", _CODEX_REPAIR)
    version = tuple(int(part) for part in match.groups())
    if version < MIN_CODEX_VERSION:
        return Prereq(
            "codex",
            False,
            f"v{'.'.join(map(str, version))} is older than required v{PINNED_CODEX_VERSION}",
            _CODEX_REPAIR,
        )
    return Prereq("codex", True, f"{output.splitlines()[0]} ({codex_path})")


def check_prerequisites() -> list[Prereq]:
    """Check the host prerequisites for running Blueprint agent tasks.

    Returns a Prereq per requirement (Node.js >= MIN_NODE_MAJOR plus runnable
    ``claude`` and ``codex`` CLIs). Never installs anything — a missing
    prerequisite carries an actionable ``fix`` string the caller prints verbatim.
    """
    results: list[Prereq] = []

    major = _node_major()
    if major is None:
        results.append(
            Prereq(
                "node",
                False,
                "not found on PATH",
                f"Install Node.js >= {MIN_NODE_MAJOR} (https://nodejs.org) — the agent binary runs on Node.",
            )
        )
    elif major < MIN_NODE_MAJOR:
        results.append(
            Prereq(
                "node",
                False,
                f"v{major} is older than the required v{MIN_NODE_MAJOR}",
                f"Upgrade Node.js to >= {MIN_NODE_MAJOR} (https://nodejs.org).",
            )
        )
    else:
        results.append(Prereq("node", True, f"v{major}"))

    claude_path = shutil.which("claude")
    if claude_path:
        results.append(Prereq("claude", True, claude_path))
    else:
        results.append(
            Prereq(
                "claude",
                False,
                "not found on PATH",
                "Install the Claude Code CLI: npm install -g @anthropic-ai/claude-code",
            )
        )

    # OpenAI credentials are assigned per task, so the absence of an OPENAI_API_KEY
    # on this host does not mean OpenAI cannot be routed here. Always require a
    # runnable Codex CLI; compute-only operators can invoke local_runner.py directly.
    results.append(_codex_prerequisite())

    return results


def resolve_url(cli_url: str | None) -> str:
    """Resolve the orchestrator URL: --url > BLUEPRINT_ORCHESTRATOR_URL > baked-in prod."""
    if cli_url and cli_url.strip():
        return cli_url.strip().rstrip("/")
    env_url = os.environ.get("BLUEPRINT_ORCHESTRATOR_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return DEFAULT_ORCHESTRATOR_URL


def _load_persisted_token() -> str | None:
    """Read the persisted runner token from the credential dir's runner_token file.

    Honors the BLUEPRINT_CREDENTIAL_DIR env override so reader and writer
    (local_runner._save_runner_token) always agree on the path.
    Returns None if the file does not exist, is empty, or cannot be read.
    """
    try:
        path = os.path.join(_credential_dir(), "runner_token")
        with open(path) as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None


def resolve_token(cli_token: str | None) -> str | None:
    """Resolve the runner token: --token > BLUEPRINT_RUNNER_TOKEN > persisted file."""
    if cli_token and cli_token.strip():
        return cli_token.strip()
    env_token = os.environ.get("BLUEPRINT_RUNNER_TOKEN", "").strip()
    if env_token:
        return env_token
    return _load_persisted_token()


def resolve_agent_image_tag(cli_tag: str | None) -> str:
    """Resolve the agent image tag: --agent-image > BLUEPRINT_AGENT_IMAGE > default.

    Mirrors resolve_url/resolve_token so an operator who already exports
    BLUEPRINT_AGENT_IMAGE (e.g. a private registry ref) keeps it, while the common
    zero-config path needs no flag at all.
    """
    if cli_tag and cli_tag.strip():
        return cli_tag.strip()
    env_tag = os.environ.get("BLUEPRINT_AGENT_IMAGE", "").strip()
    return env_tag or DEFAULT_AGENT_IMAGE_TAG


def _print_prereqs(prereqs: list[Prereq], url: str) -> None:
    print("blueprint-runner preflight", flush=True)
    print(f"  orchestrator : {url}", flush=True)
    for prereq in prereqs:
        mark = "OK     " if prereq.ok else "MISSING"
        print(f"  {prereq.name:<12}: [{mark}] {prereq.detail}", flush=True)
        if not prereq.ok and prereq.fix:
            print(f"                 -> {prereq.fix}", flush=True)


def run_local_runner(
    token: str,
    url: str,
    *,
    check: bool,
    agent_image: str | None = None,
    _run=subprocess.run,
) -> int:
    """Invoke local_runner.py with the token + URL injected into the environment.

    The token reaches the child (local_runner.py) via the environment
    (``BLUEPRINT_RUNNER_TOKEN``) rather than on its argv, so it stays out of the
    *child's* ``/proc/cmdline``. This does NOT hide the token from *this
    launcher's* own ``/proc/cmdline`` when it was started as
    ``blueprint-runner start --token <token>`` — only supplying the token via
    ``BLUEPRINT_RUNNER_TOKEN`` keeps it off the launcher's argv as well.

    When ``agent_image`` is set (the launcher ensured a usable prod agent image),
    it is exported as ``BLUEPRINT_AGENT_IMAGE`` so read-only agent tasks run in the
    docker backend. When it is None the launcher is the source of truth that the
    docker-agent path is OFF, so any *ambient* ``BLUEPRINT_AGENT_IMAGE`` is dropped
    — never leak an image the launcher deliberately did not vet (local_runner's own
    presence check is the final backstop).

    Runs it as a subprocess so signal handling and exit codes are the runner's
    own (graceful shutdown on SIGTERM/SIGINT is handled there). ``_run`` is
    injectable for tests. Returns the child's exit code.
    """
    env = dict(os.environ)
    env["BLUEPRINT_RUNNER_TOKEN"] = token
    env["BLUEPRINT_ORCHESTRATOR_URL"] = url
    if agent_image:
        env["BLUEPRINT_AGENT_IMAGE"] = agent_image
    else:
        env.pop("BLUEPRINT_AGENT_IMAGE", None)
    argv = [sys.executable, _LOCAL_RUNNER]
    if check:
        argv.append("--check")
    completed = _run(argv, env=env)
    return int(getattr(completed, "returncode", 0) or 0)


# ── Zero-config docker-agent image ────────────────────────────────────────────
#
# On `start`, if docker is usable, the launcher ensures the prod agent image
# (agent/Dockerfile) exists and hands its tag to local_runner via
# BLUEPRINT_AGENT_IMAGE, so read-only agent tasks run in the prod container
# (non-root, prod-parity) with no manual `docker build` / env-setting. Everything
# is FAIL-CLOSED: any problem leaves the image unset, which is exactly the
# pre-feature behavior (local_runner falls back to the host bwrap agent backend,
# or declines agent work). A broken/absent image is NEVER exported — that would
# drain agent tasks whose `docker run` then dies with nothing reported.


@dataclass(frozen=True)
class AgentImageResult:
    """Outcome of ensuring the docker-agent image.

    ``image`` is the ref to export as BLUEPRINT_AGENT_IMAGE (None → docker-agent
    path OFF). ``active`` says whether the docker-agent path will be live.
    ``message`` is a one-line human-readable status printed in the preflight.
    """

    image: str | None
    active: bool
    message: str


def _docker_usable(_run=subprocess.run) -> bool:
    """True iff ``docker info`` actually succeeds (docker present AND daemon reachable).

    Mirrors runner_isolation.probe_container_runtime_usable('docker'): presence on
    PATH is NOT sufficient (a denied socket must read as unusable), so we require a
    zero exit. Any failure (absent binary, denied socket, timeout) reads as unusable.
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = _run(["docker", "info"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0


def _image_present(tag: str, *, _run=subprocess.run) -> bool:
    """Whether ``docker image inspect <tag>`` succeeds (image present locally)."""
    try:
        result = _run(
            ["docker", "image", "inspect", tag], capture_output=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0


def _agent_source_revision(
    *, repo_root: str = _REPO_ROOT, _run=subprocess.run
) -> str | None:
    """Return the committed agent tree id used to invalidate local images."""
    try:
        result = _run(
            ["git", "rev-parse", "HEAD:agent"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=repo_root,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = (getattr(result, "stdout", "") or "").strip()
    if getattr(result, "returncode", 1) != 0 or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", revision
    ):
        return None
    return revision.lower()


def _image_source_revision(
    tag: str, *, _run=subprocess.run
) -> str | None:
    """Read the source-tree provenance label from an existing agent image."""
    try:
        result = _run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{_AGENT_SOURCE_LABEL}" }}}}',
                tag,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = (getattr(result, "stdout", "") or "").strip()
    if getattr(result, "returncode", 1) != 0 or revision in {"", "<no value>"}:
        return None
    return revision.lower()


def _build_image(
    tag: str,
    agent_dir: str,
    *,
    source_revision: str | None = None,
    _run=subprocess.run,
) -> bool:
    """Build ``<tag>`` from *agent_dir* (agent/Dockerfile). Streams build output.

    Returns True on a zero exit. Output is NOT captured so the operator sees build
    progress; a failure returns False so the caller declines to export a broken image.
    """
    print(
        f"[blueprint-runner] building agent image {tag} from {agent_dir} "
        "(first run only; this can take a few minutes)...",
        flush=True,
    )
    try:
        argv = ["docker", "build", "-t", tag]
        if source_revision:
            argv.extend(["--label", f"{_AGENT_SOURCE_LABEL}={source_revision}"])
        argv.append(agent_dir)
        result = _run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[blueprint-runner] docker build error: {exc}", file=sys.stderr, flush=True)
        return False
    return getattr(result, "returncode", 1) == 0


def _pull_image(ref: str, *, _run=subprocess.run) -> bool:
    """Pull ``<ref>`` from a registry. Streams pull output. True on zero exit."""
    print(f"[blueprint-runner] pulling agent image {ref}...", flush=True)
    try:
        result = _run(["docker", "pull", ref])
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[blueprint-runner] docker pull error: {exc}", file=sys.stderr, flush=True)
        return False
    return getattr(result, "returncode", 1) == 0


def _tag_image(source: str, target: str, *, _run=subprocess.run) -> bool:
    """Re-tag ``<source>`` as ``<target>`` (``docker tag``). True on zero exit."""
    try:
        result = _run(
            ["docker", "tag", source, target], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[blueprint-runner] docker tag error: {exc}", file=sys.stderr, flush=True)
        return False
    return getattr(result, "returncode", 1) == 0


def ensure_agent_image(
    *,
    tag: str,
    docker_usable: bool,
    build: bool = True,
    pull_ref: str | None = None,
    agent_dir: str = _AGENT_DIR,
    source_revision: str | None = None,
    probe_only: bool = False,
    _run=subprocess.run,
) -> AgentImageResult:
    """Ensure the prod agent image is available and return what to export.

    Fail-closed: any problem returns ``image=None`` (docker-agent path OFF), which
    is identical to the pre-feature behavior. Precedence:
      * docker unusable            → OFF (agent tasks use bwrap or are declined).
      * ``pull_ref`` given         → ``docker pull`` it (retag to ``tag`` if they
                                     differ); pull/tag failure → OFF.
      * image ``tag`` present      → ON (skip build).
      * absent + ``build``         → ``docker build`` from *agent_dir*; failure → OFF.
      * absent + ``--no-build``    → OFF.

    ``probe_only`` (used by ``check``) reports the CURRENT state without building or
    pulling — a preflight must have no side effects.
    """
    if not docker_usable:
        return AgentImageResult(
            None,
            False,
            "docker unusable -> docker-agent path OFF. Agent tasks run under host "
            "bwrap (or are declined if bwrap is unavailable).",
        )

    present = _image_present(tag, _run=_run)
    image_revision = (
        _image_source_revision(tag, _run=_run)
        if present and source_revision and not pull_ref
        else None
    )
    source_mismatch = bool(
        present and source_revision and image_revision != source_revision.lower()
    )

    if probe_only:
        if present and not source_mismatch:
            return AgentImageResult(tag, True, f"image {tag} present -> docker-agent path ON.")
        if source_mismatch:
            return AgentImageResult(
                None,
                False,
                f"image {tag} source revision is stale -> `blueprint-runner start` "
                "will rebuild it.",
            )
        if pull_ref:
            return AgentImageResult(
                None,
                False,
                f"image {tag} absent -> `blueprint-runner start` will pull {pull_ref} "
                f"(tagged {tag}).",
            )
        if build:
            return AgentImageResult(
                None,
                False,
                f"image {tag} absent -> `blueprint-runner start` will build it from {agent_dir}.",
            )
        return AgentImageResult(
            None,
            False,
            f"image {tag} absent and --no-build-image set -> docker-agent path OFF.",
        )

    # ── start: actually ensure the image is present ──
    if pull_ref:
        if not _pull_image(pull_ref, _run=_run):
            return AgentImageResult(
                None,
                False,
                f"docker pull {pull_ref} FAILED -> docker-agent path OFF "
                "(not exporting a broken image).",
            )
        if pull_ref != tag and not _tag_image(pull_ref, tag, _run=_run):
            return AgentImageResult(
                None,
                False,
                f"docker tag {pull_ref} {tag} FAILED -> docker-agent path OFF.",
            )
        return AgentImageResult(
            tag, True, f"pulled {pull_ref} (tagged {tag}) -> docker-agent path ON."
        )

    if present and not source_mismatch:
        return AgentImageResult(
            tag, True, f"image {tag} already present -> docker-agent path ON."
        )

    if not build:
        state = "stale" if source_mismatch else "absent"
        return AgentImageResult(
            None,
            False,
            f"image {tag} {state} and --no-build-image set -> docker-agent path OFF. "
            "Agent tasks run under host bwrap (or are declined).",
        )

    if not os.path.isdir(agent_dir):
        return AgentImageResult(
            None,
            False,
            f"agent build context {agent_dir!r} not found -> docker-agent path OFF.",
        )

    if not _build_image(tag, agent_dir, source_revision=source_revision, _run=_run):
        return AgentImageResult(
            None,
            False,
            f"docker build of {tag} FAILED -> docker-agent path OFF "
            "(not exporting a broken image).",
        )
    return AgentImageResult(tag, True, f"built {tag} from {agent_dir} -> docker-agent path ON.")


def _print_docker_agent_status(result: AgentImageResult) -> None:
    """Print the one-line docker-agent path status, aligned with the prereq block."""
    mark = "ON " if result.active else "OFF"
    print(f"  {'docker-agent':<12}: [{mark}    ] {result.message}", flush=True)


# ── install / uninstall (systemd service automation) ──────────────────────────
#
# `blueprint-runner install` renders the unit template with the REAL absolute
# paths resolved on this host (this interpreter, this checkout's local_runner.py,
# the ~/.blueprint credential dir), writes it to the correct systemd location,
# enables it, and turns on boot persistence (linger for --user, multi-user.target
# for --system). Every host interaction goes through an injectable ``_run`` so
# tests never touch real systemd. Fail-closed: a missing template, an absent
# systemd, or any non-zero systemctl step returns rc=1 with an actionable message
# — the installer never claims success when a step failed.


def _runner_instance() -> str | None:
    value = os.environ.get("BLUEPRINT_RUNNER_INSTANCE", "").strip()
    if not value:
        return None
    if not _RUNNER_INSTANCE_RE.fullmatch(value):
        raise ValueError(
            "BLUEPRINT_RUNNER_INSTANCE must use 1-48 lowercase letters, digits, "
            "or internal hyphens"
        )
    return value


def _runner_service_unit_name() -> str:
    instance = _runner_instance()
    return SERVICE_UNIT_NAME if instance is None else f"blueprint-runner-{instance}.service"


def _credential_dir() -> str:
    """The Blueprint credential dir — mirrors local_runner._credential_dir().

    Defaults to ``~/.blueprint`` and honors ``BLUEPRINT_CREDENTIAL_DIR`` so the
    env file the service reads lands in the same place local_runner persists the
    token to (``runner_token``).
    """
    configured = os.environ.get("BLUEPRINT_CREDENTIAL_DIR", "").strip()
    if configured:
        return configured
    instance = _runner_instance()
    suffix = "" if instance is None else f"-{instance}"
    return os.path.expanduser(f"~/.blueprint{suffix}")


def _env_file_path() -> str:
    """Absolute path of the EnvironmentFile the installed unit loads."""
    return os.path.join(_credential_dir(), "runner.env")


def _user_unit_path() -> str:
    return os.path.expanduser(f"~/.config/systemd/user/{_runner_service_unit_name()}")


def _system_unit_path() -> str:
    return f"/etc/systemd/system/{_runner_service_unit_name()}"


def _install_user() -> str:
    """The account the runner should run as.

    ``SUDO_USER`` wins so ``sudo blueprint-runner install --system`` targets the
    real operator, not root; otherwise the current user.
    """
    return (
        os.environ.get("SUDO_USER", "").strip()
        or os.environ.get("USER", "").strip()
        or getpass.getuser()
    )


def render_unit(template_path: str, subs: dict[str, str]) -> str:
    """Render a systemd unit template by literal ``@TOKEN@`` substitution.

    Raises ``FileNotFoundError`` if the template is missing and ``ValueError`` if
    any ``@…@`` placeholder is left unsubstituted — a rendered unit carrying a
    leftover token is a silently-broken service, so fail loudly instead.
    """
    with open(template_path, encoding="utf-8") as fh:
        text = fh.read()
    for token, value in subs.items():
        text = text.replace(token, value)
    leftover = sorted(set(re.findall(r"@[A-Z_]+@", text)))
    if leftover:
        raise ValueError(
            f"unsubstituted placeholder(s) {leftover} in unit rendered from {template_path}"
        )
    return text


def _systemctl(args: list[str], *, system: bool, _run=subprocess.run):
    """Run ``systemctl`` (system manager) or ``systemctl --user`` with *args*."""
    argv = ["systemctl"] if system else ["systemctl", "--user"]
    argv.extend(args)
    return _run(argv, capture_output=True, text=True)


def _systemd_available(*, system: bool, _run=subprocess.run) -> bool:
    """Whether a systemd manager we can install into is reachable on this host.

    ``systemctl [--user] is-system-running`` exits non-zero for degraded/starting
    states but still prints a state word when the manager is reachable; only a
    bus-connection failure (no user manager under this session, no system pid-1
    systemd) means systemd is genuinely unavailable here.
    """
    if shutil.which("systemctl") is None:
        return False
    try:
        result = _systemctl(["is-system-running"], system=system, _run=_run)
    except (OSError, subprocess.SubprocessError):
        return False
    stderr = getattr(result, "stderr", "") or ""
    if isinstance(stderr, str) and (
        "Failed to connect to bus" in stderr or "Failed to get D-Bus" in stderr
    ):
        return False
    return True


def _print_no_systemd_guidance(*, system: bool) -> None:
    """Actionable fallback when systemd is absent — never crash, guide instead."""
    scope = "--system" if system else "--user"
    print(
        f"ERROR: no reachable systemd manager for `install {scope}` on this host.",
        file=sys.stderr,
    )
    print(
        "  systemd was not detected (no `systemctl`, or no user/system manager "
        "on this session — e.g. a bare container or a non-systemd init).",
        file=sys.stderr,
    )
    print(
        "  Run the runner under your own supervisor instead, e.g.:",
        file=sys.stderr,
    )
    print(
        f"    nohup {sys.executable} {_LOCAL_RUNNER} >~/blueprint-runner.log 2>&1 &",
        file=sys.stderr,
    )
    print(
        "  (or `blueprint-runner start --token <token>` in a tmux/screen session).",
        file=sys.stderr,
    )


def _read_env_file(path: str) -> dict[str, str]:
    """Parse key=value pairs from a runner.env file, skipping blank lines and comments."""
    result: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    result[key.strip()] = val.strip()
    except OSError:
        pass
    return result


def _write_env_file(
    path: str,
    *,
    url: str,
    token: str | None,
    resource_limits: dict[str, str] | None = None,
) -> None:
    """Seed the 0600 EnvironmentFile so the service has its config on first start.

    The orchestrator URL is required (local_runner._require_env). The token is
    written only when supplied — after the first successful start local_runner
    persists it to ``runner_token`` and the service reads it from there, so we
    prefer not to keep it here indefinitely, but seeding it makes a fresh install
    connect on its very first boot with no manual step.

    ``resource_limits`` maps env-var name to value string for any resource-limit
    knobs the operator supplied at install time (all optional).
    """
    cred_dir = os.path.dirname(path)
    os.makedirs(cred_dir, exist_ok=True)
    try:
        os.chmod(cred_dir, 0o700)
    except OSError:
        pass
    lines = [
        "# Written by `blueprint-runner install`. Mode 0600 — do not commit.\n",
        f"BLUEPRINT_ORCHESTRATOR_URL={url}\n",
    ]
    instance = _runner_instance()
    if instance:
        lines.extend(
            [
                f"BLUEPRINT_RUNNER_INSTANCE={instance}\n",
                f"BLUEPRINT_CREDENTIAL_DIR={cred_dir}\n",
                f"BLUEPRINT_RUNNER_WORKSPACE_ROOT=/tmp/blueprint-agent-workspaces-{instance}\n",
            ]
        )
    if token:
        lines.append(f"BLUEPRINT_RUNNER_TOKEN={token}\n")
    if resource_limits:
        for env_var, value in resource_limits.items():
            lines.append(f"{env_var}={value}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def do_install(
    *,
    system: bool = False,
    token: str | None = None,
    url: str,
    dry_run: bool = False,
    resource_limits: dict[str, str] | None = None,
    no_agent_deps: bool = False,
    _run=subprocess.run,
) -> int:
    """Render, install, enable, and persist the runner service. Returns rc.

    On a successful install (not dry-run), also bootstraps the agent's node
    dependencies as a best-effort, non-fatal step — the operator can skip it
    with ``no_agent_deps=True``.
    """
    template = _SYSTEM_SERVICE_TEMPLATE if system else _USER_SERVICE_TEMPLATE
    if not os.path.isfile(template):
        print(f"ERROR: unit template not found: {template}", file=sys.stderr)
        return 1

    if not _systemd_available(system=system, _run=_run):
        _print_no_systemd_guidance(system=system)
        return 1

    env_file = _env_file_path()
    service_unit_name = _runner_service_unit_name()
    subs = {
        "@PYTHON@": sys.executable,
        "@LOCAL_RUNNER@": _LOCAL_RUNNER,
        "@ENV_FILE@": env_file,
    }
    if system:
        subs["@USER@"] = _install_user()
        subs["@WANTED_BY@"] = _SYSTEM_WANTED_BY
    try:
        unit_text = render_unit(template, subs)
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not render unit: {exc}", file=sys.stderr)
        return 1

    unit_path = _system_unit_path() if system else _user_unit_path()

    if dry_run:
        print(f"[dry-run] would seed env file: {env_file}", flush=True)
        print(f"[dry-run] would write unit:    {unit_path}", flush=True)
        print(f"[dry-run] would run: systemctl {'' if system else '--user '}"
              f"daemon-reload / enable --now {service_unit_name}", flush=True)
        if not system:
            print(f"[dry-run] would run: loginctl enable-linger {_install_user()}", flush=True)
        print("\n" + unit_text, flush=True)
        return 0

    _write_env_file(env_file, url=url, token=token, resource_limits=resource_limits)

    try:
        os.makedirs(os.path.dirname(unit_path), exist_ok=True)
        with open(unit_path, "w", encoding="utf-8") as fh:
            fh.write(unit_text)
    except OSError as exc:
        print(f"ERROR: could not write unit file {unit_path}: {exc}", file=sys.stderr)
        return 1
    print(f"[blueprint-runner] wrote unit: {unit_path}", flush=True)

    for args, label in (
        (["daemon-reload"], "daemon-reload"),
        (["enable", "--now", service_unit_name], "enable --now"),
    ):
        result = _systemctl(args, system=system, _run=_run)
        if getattr(result, "returncode", 1) != 0:
            print(
                f"ERROR: `systemctl {'' if system else '--user '}{label}` failed "
                f"(rc={getattr(result, 'returncode', '?')}). "
                f"{(getattr(result, 'stderr', '') or '').strip()}",
                file=sys.stderr,
            )
            return 1

    if not system:
        # Linger so the user service survives logout/reboot with no login session.
        # Boot persistence is the whole point of installing, so surface a failure
        # loudly — but it is not fatal: the unit is already enabled and running,
        # linger only governs starting before the operator logs in.
        try:
            linger = _run(
                ["loginctl", "enable-linger", _install_user()],
                capture_output=True,
                text=True,
            )
            linger_rc = getattr(linger, "returncode", 1)
        except (OSError, subprocess.SubprocessError) as exc:
            linger_rc = 1
            print(f"WARNING: loginctl enable-linger error: {exc}", file=sys.stderr)
        if linger_rc != 0:
            print(
                "WARNING: `loginctl enable-linger` did not succeed — the runner is "
                "installed and running, but may not start at boot until you log in. "
                f"Retry: loginctl enable-linger {_install_user()}",
                file=sys.stderr,
            )

    # Verify the unit actually came up — never claim success on a dead unit.
    active = _systemctl(["is-active", service_unit_name], system=system, _run=_run)
    enabled = _systemctl(["is-enabled", service_unit_name], system=system, _run=_run)
    if getattr(active, "returncode", 1) != 0 or getattr(enabled, "returncode", 1) != 0:
        print(
            "ERROR: the unit did not come up active AND enabled after install "
            f"(is-active rc={getattr(active, 'returncode', '?')}, "
            f"is-enabled rc={getattr(enabled, 'returncode', '?')}). Inspect: "
            f"journalctl {'' if system else '--user '}-u {service_unit_name} -n 50",
            file=sys.stderr,
        )
        return 1

    scope = "--system" if system else "--user"
    follow = f"journalctl {'' if system else '--user '}-u {service_unit_name} -f"
    print(
        f"[blueprint-runner] installed and started ({scope}). "
        f"The runner is active and enabled at boot.\n"
        f"  Unit:   {unit_path}\n"
        f"  Env:    {env_file}\n"
        f"  Logs:   {follow}",
        flush=True,
    )

    # Best-effort agent deps bootstrap (non-fatal, warn-and-continue).
    _bootstrap_agent_deps(no_agent_deps=no_agent_deps, _run=_run)

    return 0


def do_uninstall(*, system: bool = False, dry_run: bool = False, _run=subprocess.run) -> int:
    """Stop, disable, and remove the runner unit. Idempotent; credentials kept."""
    unit_path = _system_unit_path() if system else _user_unit_path()
    service_unit_name = _runner_service_unit_name()

    if dry_run:
        print(f"[dry-run] would run: systemctl {'' if system else '--user '}"
              f"disable --now {service_unit_name}", flush=True)
        print(f"[dry-run] would remove unit: {unit_path}", flush=True)
        return 0

    unit_present = os.path.isfile(unit_path)
    if not unit_present:
        print(
            f"[blueprint-runner] nothing to uninstall — no unit at {unit_path}.",
            flush=True,
        )

    # `disable --now` stops + disables. It is harmless (and idempotent) even if
    # the unit is already gone, so run it regardless; we only care that a
    # PRESENT unit is removed cleanly. Do NOT touch credential files.
    if shutil.which("systemctl") is not None:
        _systemctl(["disable", "--now", service_unit_name], system=system, _run=_run)

    removed = False
    if unit_present:
        try:
            os.remove(unit_path)
            removed = True
            print(f"[blueprint-runner] removed unit: {unit_path}", flush=True)
        except OSError as exc:
            print(f"ERROR: could not remove unit file {unit_path}: {exc}", file=sys.stderr)
            return 1

    if removed and shutil.which("systemctl") is not None:
        _systemctl(["daemon-reload"], system=system, _run=_run)

    print(
        "[blueprint-runner] uninstall complete "
        "(credential files under ~/.blueprint were left untouched).",
        flush=True,
    )
    return 0


def _livekit_system_unit_path() -> str:
    return os.path.join("/etc", "systemd", "system", LIVEKIT_SERVICE_UNIT_NAME)


def livekit_gateway_role_is_selected(
    *,
    marker_path: str = LIVEKIT_HOST_ROLE_MARKER,
    _run=subprocess.run,
) -> bool:
    """Return whether this machine is reserved for the LiveKit gateway role.

    The root-owned marker is the durable source of truth. The active-unit check
    also fails closed if an operator removes the marker while the gateway is
    still running.
    """
    if os.path.isfile(marker_path):
        return True
    if sys.platform != "linux":
        return False
    try:
        active = _run(
            ["systemctl", "is-active", "--quiet", LIVEKIT_SERVICE_UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(active, "returncode", 1) == 0


def _is_wsl_host() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _active_local_runner_pids() -> list[str]:
    """Return Linux processes that still execute Blueprint's task daemon."""
    pids: list[str] = []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "local_runner.py" in cmdline:
            pids.append(entry)
    return pids


def _livekit_host_is_supported() -> bool:
    """Reject non-Linux and WSL public-production installs before side effects."""
    if sys.platform != "linux":
        print("ERROR: LiveKit requires an always-on Linux host; this host is not Linux.", file=sys.stderr)
        return False
    if _is_wsl_host():
        print(
            "ERROR: refusing a LiveKit install on WSL. WSL2 NAT is not a reliable "
            "WebRTC/TURN host; select an always-on Linux VM instead.",
            file=sys.stderr,
        )
        return False
    return True


def _livekit_requirements() -> tuple[str, str, str, str, str] | None:
    """Return resolved host-tool paths, or print actionable error."""
    bash = shutil.which("bash")
    docker = shutil.which("docker")
    gcloud = shutil.which("gcloud")
    curl = shutil.which("curl")
    openssl = shutil.which("openssl")
    missing = [
        name for name, value in (
            ("bash", bash), ("docker", docker), ("gcloud", gcloud),
            ("curl", curl), ("openssl", openssl),
        )
        if value is None
    ]
    if missing:
        print(
            "ERROR: cannot install the LiveKit companion; missing host prerequisite(s): "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return None
    return bash, docker, gcloud, curl, openssl


def _render_livekit_unit() -> str:
    requirements = _livekit_requirements()
    if requirements is None:
        raise RuntimeError("missing LiveKit companion prerequisites")
    bash, docker, gcloud, _curl, _openssl = requirements
    return render_unit(
        _LIVEKIT_SYSTEM_SERVICE_TEMPLATE,
        {
            "@LIVEKIT_DIR@": _LIVEKIT_DIR,
            "@BASH@": bash,
            "@DOCKER@": docker,
            "@GCLOUD@": gcloud,
        },
    )


def _livekit_config_is_ready(config_dir: str = "/etc/blueprint-livekit") -> bool:
    """Require root-owned config without parsing or displaying its secret data."""
    missing = [
        path for path in (
            os.path.join(config_dir, "secret-refs"),
            os.path.join(config_dir, "public.env"),
        )
        if not os.path.isfile(path)
    ]
    if missing:
        print(
            "ERROR: LiveKit configuration is incomplete. Create the root-owned "
            "secret-refs and public.env files described in infra/livekit/README.md before enabling it. "
            f"Missing: {', '.join(missing)}",
            file=sys.stderr,
        )
        return False
    for path in (config_dir, *[os.path.join(config_dir, name) for name in ("secret-refs", "public.env")]):
        try:
            details = os.stat(path)
        except OSError as exc:
            print(f"ERROR: could not inspect LiveKit configuration {path}: {exc}", file=sys.stderr)
            return False
        if details.st_uid != 0 or (details.st_mode & 0o077):
            print(
                f"ERROR: {path} must be root-owned and inaccessible to group/other (expected 0700 dir, 0600 files).",
                file=sys.stderr,
            )
            return False
    return True


def do_livekit_preflight(*, system: bool, _run=subprocess.run) -> int:
    """Check the selected host role without starting a public service."""
    if not system:
        print("ERROR: LiveKit preflight is defined for the root-owned system service; pass --system.", file=sys.stderr)
        return 1
    if not _livekit_host_is_supported():
        return 1
    if not _systemd_available(system=True, _run=_run):
        _print_no_systemd_guidance(system=True)
        return 1
    runner_active = _systemctl(["is-active", SERVICE_UNIT_NAME], system=True, _run=_run)
    if getattr(runner_active, "returncode", 1) == 0:
        print(
            "ERROR: refusing to co-host LiveKit with an active Blueprint system runner. "
            "Choose a zero-task-capacity voice-gateway host instead.",
            file=sys.stderr,
        )
        return 1
    active_pids = _active_local_runner_pids()
    if active_pids:
        print(
            "ERROR: refusing to co-host LiveKit with active Blueprint task daemon "
            f"process(es): {', '.join(active_pids)}. Choose a zero-task-capacity voice-gateway host.",
            file=sys.stderr,
        )
        return 1
    requirements = _livekit_requirements()
    if requirements is None:
        return 1
    if not _livekit_config_is_ready():
        return 1
    bash, docker, _gcloud, _curl, _openssl = requirements
    try:
        rendered = _run(
            [bash, os.path.join(_LIVEKIT_DIR, "bin", "render-livekit-secrets"), "--check"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: could not validate LiveKit secret material: {exc}", file=sys.stderr)
        return 1
    if getattr(rendered, "returncode", 1) != 0:
        print(
            "ERROR: LiveKit secret material or TURN host contract is invalid. "
            "Inspect the root-owned secret references and public.env.",
            file=sys.stderr,
        )
        return 1
    try:
        result = _run([docker, "info"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: could not verify Docker daemon: {exc}", file=sys.stderr)
        return 1
    if getattr(result, "returncode", 1) != 0:
        print("ERROR: Docker daemon is not usable by root on this host.", file=sys.stderr)
        return 1
    print("[blueprint-runner] LiveKit preflight passed (always-on Linux host role).", flush=True)
    return 0


def do_livekit_install(*, system: bool, dry_run: bool = False, _run=subprocess.run) -> int:
    """Install the independent root-owned LiveKit system service."""
    if not system:
        print("ERROR: LiveKit may only be installed as a root-owned system service; pass --system.", file=sys.stderr)
        return 1
    if not os.path.isfile(_LIVEKIT_SYSTEM_SERVICE_TEMPLATE) or not os.path.isdir(_LIVEKIT_DIR):
        print("ERROR: LiveKit companion files are absent from this checkout.", file=sys.stderr)
        return 1
    if not _livekit_host_is_supported():
        return 1
    try:
        unit_text = _render_livekit_unit()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: could not render LiveKit companion unit: {exc}", file=sys.stderr)
        return 1

    unit_path = _livekit_system_unit_path()
    if dry_run:
        print("[dry-run] would require root-owned /etc/blueprint-livekit/{secret-refs,public.env}", flush=True)
        print(f"[dry-run] would select host role: {LIVEKIT_HOST_ROLE_MARKER}", flush=True)
        print(f"[dry-run] would write unit:     {unit_path}", flush=True)
        print(f"[dry-run] would run: systemctl daemon-reload / enable --now {LIVEKIT_SERVICE_UNIT_NAME}", flush=True)
        print("\n" + unit_text, flush=True)
        return 0

    if os.geteuid() != 0:
        print("ERROR: LiveKit system install requires root (run with sudo).", file=sys.stderr)
        return 1
    if do_livekit_preflight(system=True, _run=_run) != 0:
        return 1
    try:
        with open(LIVEKIT_HOST_ROLE_MARKER, "w", encoding="utf-8") as fh:
            fh.write("blueprint-livekit-gateway-v1\n")
        os.chmod(LIVEKIT_HOST_ROLE_MARKER, 0o444)
        os.makedirs(os.path.dirname(unit_path), exist_ok=True)
        with open(unit_path, "w", encoding="utf-8") as fh:
            fh.write(unit_text)
    except OSError as exc:
        print(f"ERROR: could not select/install LiveKit gateway host role: {exc}", file=sys.stderr)
        return 1

    for args, label in ((["daemon-reload"], "daemon-reload"), (["enable", "--now", LIVEKIT_SERVICE_UNIT_NAME], "enable --now")):
        result = _systemctl(args, system=True, _run=_run)
        if getattr(result, "returncode", 1) != 0:
            print(
                f"ERROR: `systemctl {label}` failed "
                f"(rc={getattr(result, 'returncode', '?')}). "
                f"{(getattr(result, 'stderr', '') or '').strip()}",
                file=sys.stderr,
            )
            return 1
    active = _systemctl(["is-active", LIVEKIT_SERVICE_UNIT_NAME], system=True, _run=_run)
    if getattr(active, "returncode", 1) != 0:
        print(
            "ERROR: LiveKit companion did not become active. Inspect: "
            f"journalctl -u {LIVEKIT_SERVICE_UNIT_NAME} -n 100",
            file=sys.stderr,
        )
        return 1
    print(
        f"[blueprint-runner] LiveKit companion active and enabled. "
        f"Logs: journalctl -u {LIVEKIT_SERVICE_UNIT_NAME} -f",
        flush=True,
    )
    return 0


def do_livekit_reload(*, system: bool, dry_run: bool = False, _run=subprocess.run) -> int:
    """Reload the checked-out gateway after the operator has ended active calls."""
    if not system:
        print("ERROR: LiveKit reload requires the root-owned system service; pass --system.", file=sys.stderr)
        return 1
    unit_path = _livekit_system_unit_path()
    if not os.path.isfile(unit_path):
        print(
            f"ERROR: LiveKit companion is not installed at {unit_path}. Run `blueprint-runner livekit-install --system` first.",
            file=sys.stderr,
        )
        return 1
    if not _livekit_host_is_supported():
        return 1
    try:
        unit_text = _render_livekit_unit()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: could not render LiveKit companion unit: {exc}", file=sys.stderr)
        return 1
    if dry_run:
        print(f"[dry-run] would rewrite unit: {unit_path}", flush=True)
        print(f"[dry-run] would run: systemctl daemon-reload / restart {LIVEKIT_SERVICE_UNIT_NAME}", flush=True)
        return 0
    if os.geteuid() != 0:
        print("ERROR: LiveKit system reload requires root (run with sudo).", file=sys.stderr)
        return 1
    if do_livekit_preflight(system=True, _run=_run) != 0:
        return 1
    try:
        with open(unit_path, "w", encoding="utf-8") as fh:
            fh.write(unit_text)
    except OSError as exc:
        print(f"ERROR: could not update LiveKit companion unit: {exc}", file=sys.stderr)
        return 1
    for args, label in ((["daemon-reload"], "daemon-reload"), (["restart", LIVEKIT_SERVICE_UNIT_NAME], "restart"), (["is-active", LIVEKIT_SERVICE_UNIT_NAME], "is-active")):
        result = _systemctl(args, system=True, _run=_run)
        if getattr(result, "returncode", 1) != 0:
            print(
                f"ERROR: `systemctl {label}` failed "
                f"(rc={getattr(result, 'returncode', '?')}). "
                f"{(getattr(result, 'stderr', '') or '').strip()}",
                file=sys.stderr,
            )
            return 1
    print("[blueprint-runner] LiveKit companion reloaded and healthy.", flush=True)
    return 0


def do_livekit_status(*, system: bool, _run=subprocess.run) -> int:
    """Report the independent service state without consulting runner state."""
    if not system:
        print("ERROR: LiveKit status is only defined for the system service; pass --system.", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        print("ERROR: LiveKit system status requires root (run with sudo).", file=sys.stderr)
        return 1
    if not _systemd_available(system=True, _run=_run):
        _print_no_systemd_guidance(system=True)
        return 1
    active = _systemctl(["is-active", LIVEKIT_SERVICE_UNIT_NAME], system=True, _run=_run)
    enabled = _systemctl(["is-enabled", LIVEKIT_SERVICE_UNIT_NAME], system=True, _run=_run)
    print("blueprint-livekit status", flush=True)
    print(f"  active  : {(getattr(active, 'stdout', '') or '').strip() or 'inactive'}", flush=True)
    print(f"  enabled : {(getattr(enabled, 'stdout', '') or '').strip() or 'disabled'}", flush=True)
    if getattr(active, "returncode", 1) != 0:
        return 1
    bash = shutil.which("bash")
    if bash is None:
        print("ERROR: bash is unavailable; cannot run LiveKit health check.", file=sys.stderr)
        return 1
    try:
        health = _run(
            [bash, os.path.join(_LIVEKIT_DIR, "bin", "livekit-healthcheck")],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: could not run LiveKit health check: {exc}", file=sys.stderr)
        return 1
    if getattr(health, "returncode", 1) != 0:
        print("  health  : FAILED (inspect journalctl -u blueprint-livekit.service)", flush=True)
        return 1
    print("  health  : OK", flush=True)
    return 0


def _restart_runner_service(*, _run=subprocess.run) -> bool | None:
    """Restart the runner systemd service if it is installed and active.

    Checks both user and system scopes, restarting the first active unit
    found. User scope is checked first (the common case for a local runner
    installed by a non-root operator).

    Returns True when restart succeeds, False when an active unit's restart
    fails, and None when there is no active installed service to restart.
    """
    if shutil.which("systemctl") is None:
        return None

    service_unit_name = _runner_service_unit_name()
    for system in (False, True):
        unit_path = _system_unit_path() if system else _user_unit_path()
        if not os.path.isfile(unit_path):
            continue
        result = _systemctl(["is-active", service_unit_name], system=system, _run=_run)
        if getattr(result, "returncode", 1) == 0:
            scope_label = "system" if system else "user"
            print(
                f"[blueprint-runner] restarting {service_unit_name} "
                f"({scope_label} scope)...",
                flush=True,
            )
            restart = _systemctl(
                ["restart", service_unit_name], system=system, _run=_run
            )
            if getattr(restart, "returncode", 1) == 0:
                return True
            print(
                f"ERROR: failed to restart {service_unit_name} "
                f"({scope_label} scope).",
                file=sys.stderr,
                flush=True,
            )
            return False
    return None


# ── agent deps bootstrap (install-time npm ci) ─────────────────────────────────
#
# `blueprint-runner install` now bootstraps the agent's node dependencies so the
# runner can claim agent tasks immediately without the operator knowing to run
# `cd agent && npm install` separately. Best-effort, non-fatal — a failure or
# absent node/npm is a WARNING, not an install abort. The `--no-agent-deps` flag
# opts out for operators who manage deps on their own cycle.


def _bootstrap_agent_deps(
    *,
    agent_dir: str = _AGENT_DIR,
    no_agent_deps: bool = False,
    _run=subprocess.run,
) -> None:
    """Install agent node dependencies (npm ci) if the agent source is present.

    Best-effort and NON-FATAL: a missing npm, a failed install, or a timeout
    prints a WARNING but never aborts the caller. An operator who manages
    dependencies on their own cycle passes ``--no-agent-deps`` to skip.

    Gating (all must hold):
      * ``no_agent_deps`` is False.
      * ``agent_dir`` is a directory with ``package.json`` (agent project root).
      * ``agent/src/index.ts`` exists (agent source — not a compute-only runner).
      * ``agent/dist/index.js`` is absent (no pre-built artifact — deps needed
        for the tsx-based dev-server path).
    """
    if no_agent_deps:
        return

    if not os.path.isdir(agent_dir):
        return  # compute-only runner, nothing to do

    if not os.path.isfile(os.path.join(agent_dir, "package.json")):
        return  # not an agent project root

    if not os.path.isfile(os.path.join(agent_dir, "src", "index.ts")):
        return  # agent source absent — compute-only runner

    if os.path.isfile(os.path.join(agent_dir, "dist", "index.js")):
        return  # pre-built artifact present — deps already installed

    if shutil.which("npm") is None:
        print(
            "[blueprint-runner] WARNING: npm not found on PATH — cannot install agent "
            "dependencies. Agent tasks will be declined until Node.js + npm are "
            "installed and `cd agent && npm install` has been run.",
            file=sys.stderr, flush=True,
        )
        return

    print(
        f"[blueprint-runner] installing agent node dependencies (npm ci in "
        f"{agent_dir})...",
        flush=True,
    )
    try:
        result = _run(
            ["npm", "ci"], cwd=agent_dir, timeout=120,
        )
        if result.returncode != 0:
            print(
                f"WARNING: npm ci in {agent_dir} exited {result.returncode}. "
                "Agent tasks may be declined until dependencies are installed "
                "manually (`cd agent && npm install`).",
                file=sys.stderr, flush=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"WARNING: could not run npm ci in {agent_dir}: {exc}. "
            "Agent tasks may be declined until dependencies are installed "
            "manually.",
            file=sys.stderr, flush=True,
        )


# ── update (efficient self-update via git) ─────────────────────────────────────
#
# `blueprint-runner update` fetches the latest commits from origin, fast-forwards
# the local branch if it is behind, and conditionally reinstalls npm dependencies
# only when a lockfile changed — no full reinstall on every update.


def _git(args: list[str], *, cwd: str, _run=subprocess.run) -> subprocess.CompletedProcess | None:
    """Run a git subcommand and return the result, or None on any failure.

    Returns None (never raises) when git is absent, the cwd is not a repo, or a
    timeout / OSError occurs — a failure here must never crash the launcher.
    """
    try:
        return _run(
            ["git"] + args,
            capture_output=True, text=True, timeout=30,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def do_update(
    *,
    repo_root: str = _REPO_ROOT,
    restart_service: bool = True,
    target_commit: str | None = None,
    _transition_path: str | None = None,
    _run=subprocess.run,
) -> int:
    """Update the runner checkout without conflating branch and release authority.

    With ``target_commit``, fetch and verify that exact authenticated served SHA,
    then replace a clean worktree with a detached checkout. This deliberately
    supports squash-merge divergence and intentional rollback while never
    moving a local branch pointer. Without a target, preserve the conventional
    tracked-branch fast-forward updater used by the manual CLI.

    Returns 0 on success or nothing-to-do, 1 on any failure.
    """
    print("[blueprint-runner] checking for updates...", flush=True)
    if target_commit is not None:
        if not runner_exact_update.update_to_exact_commit(
            repo_root=repo_root,
            target_commit=target_commit,
            transition_path=_transition_path,
            run=_run,
        ):
            return 1
        if restart_service:
            restart_result = _restart_runner_service(_run=_run)
            if restart_result is False:
                return 1
        print("[blueprint-runner] update complete.", flush=True)
        return 0

    # Normal no-target updater: retain branch/upstream fast-forward semantics.
    fetch = _git(["fetch", "origin"], cwd=repo_root, _run=_run)
    if fetch is None or fetch.returncode != 0:
        print("ERROR: could not run git — is this a git repository?", file=sys.stderr, flush=True)
        return 1

    head_result = _git(["rev-parse", "HEAD"], cwd=repo_root, _run=_run)
    if head_result is None or head_result.returncode != 0:
        print("ERROR: could not resolve HEAD — not a git repository?", file=sys.stderr, flush=True)
        return 1
    old_head = head_result.stdout.strip()

    branch_result = _git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, _run=_run
    )
    branch = (
        branch_result.stdout.strip()
        if branch_result is not None and branch_result.returncode == 0
        else ""
    )

    upstream_ref = "@{u}"
    upstream_result = _git(["rev-parse", "@{u}"], cwd=repo_root, _run=_run)
    if upstream_result is None or upstream_result.returncode != 0:
        if branch != "HEAD":
            print(
                "ERROR: no upstream tracking branch configured. Push first, then update.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        upstream_ref = "refs/remotes/origin/main"
        detached_fetch = _git(
            [
                "fetch",
                "--no-tags",
                "origin",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
            cwd=repo_root,
            _run=_run,
        )
        if detached_fetch is None or detached_fetch.returncode != 0:
            print(
                "ERROR: detached checkout could not fetch origin main exactly.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        upstream_result = _git(
            ["rev-parse", "--verify", f"{upstream_ref}^{{commit}}"],
            cwd=repo_root,
            _run=_run,
        )
        if upstream_result is None or upstream_result.returncode != 0:
            print(
                "ERROR: detached checkout cannot resolve fetched origin main as a commit.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        print(
            "[blueprint-runner] detached deployment checkout — "
            "using the exact fetched origin main commit for a verified fast-forward.",
            flush=True,
        )
    upstream_sha = upstream_result.stdout.strip()

    if old_head == upstream_sha:
        print("[blueprint-runner] already up to date — nothing to pull.", flush=True)
        return 0

    merge_base = _git(["merge-base", old_head, upstream_sha], cwd=repo_root, _run=_run)
    if merge_base and merge_base.returncode == 0:
        base_sha = merge_base.stdout.strip()
        if base_sha == upstream_sha:
            print(
                "[blueprint-runner] local branch is AHEAD of origin — push first, or defer update.",
                flush=True,
            )
            return 1
        if base_sha != old_head:
            print(
                "[blueprint-runner] local and origin have DIVERGED — "
                "cannot fast-forward. Rebase or reset, then re-run update.",
                file=sys.stderr,
                flush=True,
            )
            return 1

    branch_label = branch or "current branch"
    print(f"[blueprint-runner] pulling {branch_label}: "
          f"{old_head[:10]}..{upstream_sha[:10]}", flush=True)
    merge = _git(["merge", "--ff-only", upstream_ref], cwd=repo_root, _run=_run)
    if merge is None or merge.returncode != 0:
        stderr = (merge.stderr if merge else "") or ""
        print(f"ERROR: fast-forward merge failed:\n{stderr}", file=sys.stderr, flush=True)
        return 1

    print("[blueprint-runner] updated to", upstream_sha[:10], flush=True)

    changed = _git(["diff", "--name-only", f"{old_head}..HEAD"], cwd=repo_root, _run=_run)
    if changed and changed.returncode == 0 and changed.stdout.strip():
        changed_files = changed.stdout.strip().splitlines()
        for cf in changed_files:
            if cf.endswith("/package-lock.json") or cf == "package-lock.json":
                lock_dir = os.path.dirname(os.path.join(repo_root, cf))
                print(
                    f"[blueprint-runner] {cf} changed — reinstalling npm dependencies in {lock_dir}...",
                    flush=True,
                )
                try:
                    result = _run(["npm", "ci"], cwd=lock_dir, timeout=120)
                    if result.returncode != 0:
                        print(
                            f"WARNING: npm ci in {lock_dir} exited {result.returncode}. "
                            "Agent may not work until dependencies are installed manually.",
                            file=sys.stderr, flush=True,
                        )
                except (OSError, subprocess.SubprocessError) as exc:
                    print(
                        f"WARNING: could not run npm ci in {lock_dir}: {exc}",
                        file=sys.stderr, flush=True,
                    )

    if restart_service:
        restart_result = _restart_runner_service(_run=_run)
        if restart_result is False:
            return 1

    print("[blueprint-runner] update complete.", flush=True)
    return 0


def checkout_contains_commit(
    commit: str,
    *,
    repo_root: str = _REPO_ROOT,
    _run=subprocess.run,
) -> bool:
    """Return whether the checkout's HEAD contains ``commit`` in its history.

    A deployed orchestrator can legitimately advertise a commit slightly behind
    the tracked branch tip.  Exact SHA comparison would misclassify that newer
    checkout as stale, so auto-update convergence is ancestry-based.
    """
    if not commit:
        return False
    result = _git(
        ["merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=repo_root,
        _run=_run,
    )
    return result is not None and result.returncode == 0


def do_status() -> int:
    """Read and print ``~/.blueprint/status.json`` in a human-readable summary.

    The status file is written by the runner on every heartbeat (see
    ``local_runner._write_status_json``).  It contains no secrets — only the
    runner identity, last heartbeat timestamp, active work, and detected
    isolation capabilities.

    Returns 0 on success, 1 when the file is missing or unreadable.
    """
    path = os.path.join(_credential_dir(), "status.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(
            f"ERROR: status file not found at {path}",
            file=sys.stderr,
        )
        print(
            "  The runner may not have started yet, or ~/.blueprint is empty.",
            file=sys.stderr,
        )
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR: could not parse {path}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: could not read {path}: {exc}", file=sys.stderr)
        return 1

    # ── Compute how long ago the last heartbeat was ──
    last_hb = str(data.get("last_heartbeat_at", "") or "")
    time_ago_str = ""
    hb_seconds_ago: int | None = None
    if last_hb:
        try:
            hb_dt = datetime.fromisoformat(last_hb)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - hb_dt
            seconds = int(delta.total_seconds())
            hb_seconds_ago = seconds
            if seconds < 0:
                time_ago_str = " (in the future — clock skew?)"
            elif seconds < 60:
                time_ago_str = f" ({seconds}s ago)"
            elif seconds < 3600:
                time_ago_str = f" ({seconds // 60}m {seconds % 60}s ago)"
            else:
                hrs = seconds // 3600
                mins = (seconds % 3600) // 60
                time_ago_str = f" ({hrs}h {mins}m ago)" if hrs < 24 else f" ({hrs // 24}d {hrs % 24}h ago)"
        except (ValueError, TypeError):
            time_ago_str = " (unparseable timestamp)"

    active_tasks = data.get("active_task_ids") or []
    active_compute = data.get("active_compute_job_ids") or []
    bwrap = data.get("bwrap_ok")
    docker = data.get("docker_usable")

    print("blueprint-runner status", flush=True)
    print(f"  runner_id              : {data.get('runner_id', 'N/A')}", flush=True)
    print(f"  last_heartbeat_at      : {last_hb}{time_ago_str}", flush=True)
    print(f"  last_heartbeat_status  : {data.get('last_heartbeat_status', 'N/A')}", flush=True)
    print(f"  active_task_ids        : {', '.join(str(t) for t in active_tasks) if active_tasks else 'none'}", flush=True)
    print(f"  active_compute_job_ids : {', '.join(str(j) for j in active_compute) if active_compute else 'none'}", flush=True)
    if bwrap is not None:
        print(f"  bwrap_ok               : {bwrap}", flush=True)
    if docker is not None:
        print(f"  docker_usable          : {docker}", flush=True)
    agent = data.get("agent_backend")
    compute = data.get("compute_backend")
    if agent:
        print(f"  agent_backend          : {agent}", flush=True)
    if compute:
        print(f"  compute_backend        : {compute}", flush=True)

    # Health assessment
    if hb_seconds_ago is not None:
        if hb_seconds_ago > 120:
            print(f"  health                 : STALE (no heartbeat in {hb_seconds_ago}s)", flush=True)
        else:
            print("  health                 : OK", flush=True)

    # ── Resource-limit config (from runner.env) ──
    env_file = _env_file_path()
    env_vars = _read_env_file(env_file)
    env_file_found = bool(env_vars) or os.path.exists(env_file)
    print("", flush=True)
    print("resource limits (from runner.env):", flush=True)
    if not env_file_found:
        print(f"  (env file not found at {env_file} — runner may not be installed as a service)", flush=True)
    for _flag, env_var, _dest, _help in _RESOURCE_LIMIT_VARS:
        value = env_vars.get(env_var)
        display = value if value is not None else "not set (using default)"
        label = env_var.lower().replace("blueprint_runner_", "").replace("_", "-")
        print(f"  {label:<34}: {display}", flush=True)

    return 0


def _post_update_result(
    url: str,
    token: str,
    *,
    status: str,
    old_version: str | None = None,
    new_version: str | None = None,
) -> None:
    """POST the update outcome to the orchestrator. Best-effort, never raises.

    ``status`` must be ``"success"`` or ``"failure"``.  Uses Python stdlib
    ``urllib`` so there is no extra dependency.  Any HTTP / network / timeout
    error is silently swallowed — the update CLI must never block on this.

    Called from ``main()`` after ``do_update()`` returns.
    """
    endpoint = f"{url}/api/runner/update-report"
    body = json.dumps(
        {
            "status": status,
            "old_version": old_version,
            "new_version": new_version,
        }
    ).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        pass  # Best-effort — never fail the update CLI over a notification


def report_update_result(
    url: str,
    token: str,
    *,
    status: str,
    old_version: str | None = None,
    new_version: str | None = None,
) -> None:
    """Public best-effort update reporter shared by CLI and daemon paths."""
    try:
        _post_update_result(
            url,
            token,
            status=status,
            old_version=old_version,
            new_version=new_version,
        )
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blueprint-runner",
        description=(
            "Start a Blueprint self-hosted runner. The orchestrator URL is baked in; "
            "you only need a token from the Blueprint console (Runtime -> Devices and runners -> Enroll runner)."
        ),
    )
    sub = parser.add_subparsers(
        dest="command", metavar="{start,check,install,uninstall,update,status,livekit-preflight,livekit-install,livekit-reload,livekit-status}"
    )
    for name, help_text in (
        ("start", "Run the runner (blocks; Ctrl-C for graceful shutdown)."),
        ("check", "Run the preflight only and exit; claims no work."),
    ):
        sub_parser = sub.add_parser(name, help=help_text)
        sub_parser.add_argument(
            "--token",
            help="Runner token (or set BLUEPRINT_RUNNER_TOKEN).",
        )
        sub_parser.add_argument(
            "--url",
            help=f"Orchestrator base URL (default: {DEFAULT_ORCHESTRATOR_URL}).",
        )
        sub_parser.add_argument(
            "--instance",
            help="Isolated runner instance name (separate credentials, workspace, and service).",
        )
        sub_parser.add_argument(
            "--agent-image",
            metavar="TAG",
            help=(
                "Local tag for the prod agent image used by the docker-agent path "
                f"(default: {DEFAULT_AGENT_IMAGE_TAG}, or BLUEPRINT_AGENT_IMAGE)."
            ),
        )
        sub_parser.add_argument(
            "--build-image",
            dest="build_image",
            action=argparse.BooleanOptionalAction,
            default=True,
            help=(
                "Build the agent image from agent/Dockerfile if it is absent "
                "(default: build if absent; --no-build-image to disable)."
            ),
        )
        sub_parser.add_argument(
            "--pull",
            metavar="REF",
            help=(
                "Pull this registry ref for the agent image instead of building "
                "(retagged to --agent-image if they differ). Implies --docker-agent."
            ),
        )
        sub_parser.add_argument(
            "--docker-agent",
            dest="docker_agent",
            action="store_true",
            default=False,
            help=(
                "Enable the docker-agent path: ensure the prod agent image and pass it "
                "to local_runner so read-only agent tasks run in the prod container. "
                "Without this flag (the default) bwrap is used for all agent tasks. "
                "Also enabled by BLUEPRINT_AGENT_BACKEND=docker env var, or implicitly "
                "by --pull (pulling a registry image is inherently a docker opt-in)."
            ),
        )

    install_parser = sub.add_parser(
        "install",
        help=(
            "Install the runner as a durable systemd service (renders the unit, "
            "enables it, and turns on boot persistence)."
        ),
    )
    install_parser.add_argument(
        "--token",
        help="Runner token to seed into the env file (or set BLUEPRINT_RUNNER_TOKEN).",
    )
    install_parser.add_argument(
        "--url",
        help=f"Orchestrator base URL to bake into the env file (default: {DEFAULT_ORCHESTRATOR_URL}).",
    )
    install_parser.add_argument(
        "--instance",
        help="Isolated runner instance name (separate credentials, workspace, and service).",
    )
    install_parser.add_argument(
        "--system",
        action="store_true",
        default=False,
        help=(
            "Install a system unit (/etc/systemd/system, WantedBy=multi-user.target, "
            "User=$USER) instead of a systemd --user unit. Prefer this on shared boxes "
            "(e.g. a DGX) where the runner should not depend on one operator's login. "
            "Requires root (run under sudo)."
        ),
    )
    install_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print the rendered unit and the commands that would run, without touching the host.",
    )
    for _flag, _env_var, _dest, _help_text in _RESOURCE_LIMIT_VARS:
        install_parser.add_argument(_flag, dest=_dest, default=None, help=_help_text)
    install_parser.add_argument(
        "--no-agent-deps",
        dest="no_agent_deps",
        action="store_true",
        default=False,
        help="Skip installing agent node dependencies (npm ci) during install. "
             "Useful when you manage agent deps on your own cycle.",
    )

    uninstall_parser = sub.add_parser(
        "uninstall",
        help="Stop, disable, and remove the runner service (credential files are kept).",
    )
    uninstall_parser.add_argument(
        "--system",
        action="store_true",
        default=False,
        help="Uninstall the system unit instead of the --user unit (requires root).",
    )
    uninstall_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print the commands that would run, without touching the host.",
    )
    uninstall_parser.add_argument(
        "--instance",
        help="Isolated runner instance name to uninstall.",
    )

    for name, help_text in (
        (
            "livekit-preflight",
            "Check the selected always-on Linux host role before installing LiveKit.",
        ),
        (
            "livekit-install",
            "Install the independent root-owned LiveKit system service (never a task).",
        ),
        (
            "livekit-reload",
            "Reload the checked-out LiveKit companion after ending active calls.",
        ),
        (
            "livekit-status",
            "Show the independent LiveKit system-service status.",
        ),
    ):
        livekit_parser = sub.add_parser(name, help=help_text)
        livekit_parser.add_argument(
            "--system",
            action="store_true",
            default=False,
            help="Operate on the root-owned system service (required for install/reload/status).",
        )
        livekit_parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            default=False,
            help="Print the rendered unit and commands without touching the host.",
        )

    update_parser = sub.add_parser(
        "update",
        help="Update the local checkout and reinstall dependencies only when needed.",
    )
    update_parser.add_argument("--instance", help="Isolated runner instance name.")
    update_parser.add_argument(
        "--target-commit",
        metavar="SHA",
        help=(
            "Authenticated full served SHA to fetch from origin and check out "
            "detached without moving a local branch."
        ),
    )
    update_parser.add_argument(
        "--repo-root",
        metavar="PATH",
        help=(
            "Explicit runner checkout to update (trusted recovery/bootstrap use; "
            "defaults to the checkout containing this launcher)."
        ),
    )
    status_parser = sub.add_parser(
        "status",
        help="Show runner process status from ~/.blueprint/status.json.",
    )
    status_parser.add_argument("--instance", help="Isolated runner instance name.")
    return parser


def _run_update_and_report(
    *, repo_root: str = _REPO_ROOT, target_commit: str | None = None
) -> int:
    """Run ``do_update()``, capture version before/after, and report to orchestrator.

    Best-effort POST to ``/api/runner/update-report`` so the operator console
    surfaces the outcome.  A reporting failure is silently swallowed — the CLI
    returns ``do_update()``'s exit code regardless.
    """
    # Capture version before update.
    old_version: str | None = None
    repo_root = os.path.realpath(repo_root)
    head_result = _git(["rev-parse", "HEAD"], cwd=repo_root)
    if head_result and head_result.returncode == 0:
        old_version = head_result.stdout.strip()

    rc = do_update(repo_root=repo_root, target_commit=target_commit)

    # Capture version after update.
    new_version: str | None = None
    head_result = _git(["rev-parse", "HEAD"], cwd=repo_root)
    if head_result and head_result.returncode == 0:
        new_version = head_result.stdout.strip()

    # Report to orchestrator (best-effort).
    try:
        _post_update_result(
            url=resolve_url(None),
            token=resolve_token(None),
            status="success" if rc == 0 else "failure",
            old_version=old_version,
            new_version=new_version,
        )
    except Exception:
        pass

    return rc


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 2

    instance = getattr(args, "instance", None)
    if instance:
        os.environ["BLUEPRINT_RUNNER_INSTANCE"] = instance
        try:
            _runner_instance()
        except ValueError as exc:
            parser.error(str(exc))

    if args.command == "update":
        if getattr(args, "repo_root", None) and not getattr(
            args, "target_commit", None
        ):
            parser.error("--repo-root requires --target-commit")
        return _run_update_and_report(
            repo_root=getattr(args, "repo_root", None) or _REPO_ROOT,
            target_commit=getattr(args, "target_commit", None),
        )

    if args.command == "uninstall":
        return do_uninstall(
            system=getattr(args, "system", False),
            dry_run=getattr(args, "dry_run", False),
        )

    if args.command == "livekit-install":
        return do_livekit_install(
            system=getattr(args, "system", False),
            dry_run=getattr(args, "dry_run", False),
        )

    if args.command == "livekit-reload":
        return do_livekit_reload(
            system=getattr(args, "system", False),
            dry_run=getattr(args, "dry_run", False),
        )

    if args.command == "livekit-preflight":
        return do_livekit_preflight(system=getattr(args, "system", False))

    if args.command == "livekit-status":
        return do_livekit_status(system=getattr(args, "system", False))

    if args.command == "status":
        return do_status()

    if args.command == "install":
        # The token is OPTIONAL for install: if omitted, the service reads the
        # persisted ~/.blueprint/runner_token written on the runner's first start.
        resource_limits: dict[str, str] = {}
        for _flag, env_var, dest, _help in _RESOURCE_LIMIT_VARS:
            val = getattr(args, dest, None)
            if val is not None:
                resource_limits[env_var] = str(val)
        return do_install(
            system=getattr(args, "system", False),
            token=resolve_token(getattr(args, "token", None)),
            url=resolve_url(getattr(args, "url", None)),
            dry_run=getattr(args, "dry_run", False),
            resource_limits=resource_limits or None,
            no_agent_deps=getattr(args, "no_agent_deps", False),
        )

    token = resolve_token(getattr(args, "token", None))
    if not token:
        print(
            "ERROR: no runner token. Pass --token <token> or set BLUEPRINT_RUNNER_TOKEN.",
            file=sys.stderr,
        )
        print(
            "Enroll a runner in the Blueprint console (Runtime -> Devices and runners -> Enroll runner) to get one.",
            file=sys.stderr,
        )
        return 2

    url = resolve_url(getattr(args, "url", None))

    prereqs = check_prerequisites()
    _print_prereqs(prereqs, url)
    missing = [p for p in prereqs if not p.ok]
    # Provider-specific capability failures must not take unrelated capacity
    # offline. Host local_runner starts its Blueprint-owned staged maintainer for
    # Claude/Codex, while heartbeat health keeps only the broken provider
    # ineligible until verification. Node remains a hard agent-runtime blocker.
    provider_cli_names = {"claude", "codex"}
    start_blockers = [p for p in missing if p.name not in provider_cli_names]

    if not _check_provider_credentials():
        print(
            "  [WARNING] No LLM provider credential found (ANTHROPIC_API_KEY, "
            "CLAUDE_CODE_OAUTH_TOKEN, ZAI_API_KEY, or OPENAI_API_KEY) — agent "
            "tasks that require LLM access will fail at PRE_CLAIM_FAILED. "
            "Compute-only runners are unaffected.",
            flush=True,
        )

    # Docker-agent opt-in: explicit --docker-agent flag, BLUEPRINT_AGENT_BACKEND=docker,
    # or --pull (pulling a registry image is inherently a docker operation). Without an
    # explicit opt-in the docker-agent path stays OFF: bwrap handles all agent tasks.
    # This is the deliberate bwrap-first preference: docker is never auto-enabled just
    # because docker happens to be usable on the host.
    docker_opted_in = (
        getattr(args, "docker_agent", False)
        or os.environ.get("BLUEPRINT_AGENT_BACKEND", "").strip().lower() == "docker"
        or getattr(args, "pull", None) is not None
    )
    if docker_opted_in:
        tag = resolve_agent_image_tag(getattr(args, "agent_image", None))
        image_result = ensure_agent_image(
            tag=tag,
            docker_usable=_docker_usable(),
            build=getattr(args, "build_image", True),
            pull_ref=getattr(args, "pull", None),
            source_revision=_agent_source_revision(),
            probe_only=(args.command == "check"),
        )
    else:
        image_result = AgentImageResult(
            None,
            False,
            "docker-agent path OFF (bwrap is default; pass --docker-agent or set "
            "BLUEPRINT_AGENT_BACKEND=docker to enable the prod-image path).",
        )
    _print_docker_agent_status(image_result)

    if args.command == "check":
        # Full preflight: prereqs above + local_runner's orchestrator/token/isolation
        # report. Non-zero if any prerequisite is missing OR local_runner --check fails.
        print(flush=True)
        rc = run_local_runner(token, url, check=True, agent_image=image_result.image)
        return 0 if (rc == 0 and not missing) else 1

    # start
    if start_blockers:
        print(
            "\nCannot start: install the missing prerequisite(s) listed above, then re-run.",
            file=sys.stderr,
        )
        return 1
    missing_provider_clis = [p.name for p in missing if p.name in provider_cli_names]
    if missing_provider_clis:
        print(
            "\n[WARNING] Starting with unhealthy provider CLI(s): "
            f"{', '.join(sorted(missing_provider_clis))}. A host runner will "
            "attempt a staged Blueprint-managed repair in the background; each "
            "provider remains ineligible until its version probe succeeds. Other "
            "provider and compute capacity remain available.",
            file=sys.stderr,
            flush=True,
        )
    print(flush=True)
    return run_local_runner(token, url, check=False, agent_image=image_result.image)


if __name__ == "__main__":
    sys.exit(main())
