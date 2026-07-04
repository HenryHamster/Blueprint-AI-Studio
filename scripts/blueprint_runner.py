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

Prerequisites (Node.js >= 22 and the ``claude`` CLI) are installed by the
operator the normal way. This launcher only *checks* for them and prints an
actionable message when one is missing — it never bundles or installs anything
(standard dev-tool convention). ``local_runner.py`` still enforces its own
fail-closed isolation preflight; this wrapper just makes the common path clean.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Baked-in production orchestrator base URL. Keep in sync with the admin-UI
# constant DEFAULT_ORCHESTRATOR_URL in admin-ui/src/components/FleetRunnersPanel.utils.ts
# so the enroll dialog only appends --url when the project points elsewhere.
DEFAULT_ORCHESTRATOR_URL = "https://blueprint-orchestrator-mmtfi5aysa-uc.a.run.app"

# The node agent (agent/dist/index.js) requires a modern Node runtime and spawns
# the `claude` CLI for agent tasks. These are the only two host prerequisites the
# launcher checks; everything else is handled by local_runner's own preflight.
MIN_NODE_MAJOR = 22

# Default local tag for the prod agent container image (built from agent/Dockerfile).
# On `start`, when docker is usable, the launcher ensures this image exists (builds
# it if absent) and hands it to local_runner via BLUEPRINT_AGENT_IMAGE, so read-only
# agent tasks run in the prod image (non-root, prod-parity) with ZERO operator setup.
# Overridable with --agent-image or the BLUEPRINT_AGENT_IMAGE env var.
DEFAULT_AGENT_IMAGE_TAG = "blueprint-agent:local"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_RUNNER = os.path.join(_SCRIPT_DIR, "local_runner.py")
# The prod agent image builds from <repo>/agent (agent/Dockerfile is the default
# Dockerfile in that build context). scripts/ lives directly under the repo root,
# so the agent build context is scripts/../agent.
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")


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


def check_prerequisites() -> list[Prereq]:
    """Check the host prerequisites for running Blueprint agent tasks.

    Returns a Prereq per requirement (Node.js >= MIN_NODE_MAJOR, the ``claude``
    CLI). Never installs anything — a missing prerequisite carries an actionable
    ``fix`` string the caller prints verbatim.
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

    return results


def resolve_url(cli_url: str | None) -> str:
    """Resolve the orchestrator URL: --url > BLUEPRINT_ORCHESTRATOR_URL > baked-in prod."""
    if cli_url and cli_url.strip():
        return cli_url.strip().rstrip("/")
    env_url = os.environ.get("BLUEPRINT_ORCHESTRATOR_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return DEFAULT_ORCHESTRATOR_URL


def resolve_token(cli_token: str | None) -> str | None:
    """Resolve the runner token: --token > BLUEPRINT_RUNNER_TOKEN, else None."""
    if cli_token and cli_token.strip():
        return cli_token.strip()
    env_token = os.environ.get("BLUEPRINT_RUNNER_TOKEN", "").strip()
    return env_token or None


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


def _build_image(tag: str, agent_dir: str, *, _run=subprocess.run) -> bool:
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
        result = _run(["docker", "build", "-t", tag, agent_dir])
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

    if probe_only:
        if present:
            return AgentImageResult(tag, True, f"image {tag} present -> docker-agent path ON.")
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

    if present:
        return AgentImageResult(
            tag, True, f"image {tag} already present -> docker-agent path ON."
        )

    if not build:
        return AgentImageResult(
            None,
            False,
            f"image {tag} absent and --no-build-image set -> docker-agent path OFF. "
            "Agent tasks run under host bwrap (or are declined).",
        )

    if not os.path.isdir(agent_dir):
        return AgentImageResult(
            None,
            False,
            f"agent build context {agent_dir!r} not found -> docker-agent path OFF.",
        )

    if not _build_image(tag, agent_dir, _run=_run):
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blueprint-runner",
        description=(
            "Start a Blueprint self-hosted runner. The orchestrator URL is baked in; "
            "you only need a token from the Blueprint console (Fleet -> Enroll runner)."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="{start,check}")
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
                "(retagged to --agent-image if they differ)."
            ),
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 2

    token = resolve_token(getattr(args, "token", None))
    if not token:
        print(
            "ERROR: no runner token. Pass --token <token> or set BLUEPRINT_RUNNER_TOKEN.",
            file=sys.stderr,
        )
        print(
            "Enroll a runner in the Blueprint console (Fleet -> Enroll runner) to get one.",
            file=sys.stderr,
        )
        return 2

    url = resolve_url(getattr(args, "url", None))

    prereqs = check_prerequisites()
    _print_prereqs(prereqs, url)
    missing = [p for p in prereqs if not p.ok]

    # Zero-config docker-agent path: ensure the prod agent image so read-only agent
    # tasks run in the prod container. `check` probes (no side effects); `start`
    # actually builds/pulls. Fail-closed — a None image leaves the docker-agent path
    # OFF (bwrap-or-decline), preserving the pre-feature behavior.
    tag = resolve_agent_image_tag(getattr(args, "agent_image", None))
    image_result = ensure_agent_image(
        tag=tag,
        docker_usable=_docker_usable(),
        build=getattr(args, "build_image", True),
        pull_ref=getattr(args, "pull", None),
        probe_only=(args.command == "check"),
    )
    _print_docker_agent_status(image_result)

    if args.command == "check":
        # Full preflight: prereqs above + local_runner's orchestrator/token/isolation
        # report. Non-zero if any prerequisite is missing OR local_runner --check fails.
        print(flush=True)
        rc = run_local_runner(token, url, check=True, agent_image=image_result.image)
        return 0 if (rc == 0 and not missing) else 1

    # start
    if missing:
        print(
            "\nCannot start: install the missing prerequisite(s) listed above, then re-run.",
            file=sys.stderr,
        )
        return 1
    print(flush=True)
    return run_local_runner(token, url, check=False, agent_image=image_result.image)


if __name__ == "__main__":
    sys.exit(main())
