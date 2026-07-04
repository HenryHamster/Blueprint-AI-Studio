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

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_RUNNER = os.path.join(_SCRIPT_DIR, "local_runner.py")


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
    _run=subprocess.run,
) -> int:
    """Invoke local_runner.py with the token + URL injected into the environment.

    The token reaches the child (local_runner.py) via the environment
    (``BLUEPRINT_RUNNER_TOKEN``) rather than on its argv, so it stays out of the
    *child's* ``/proc/cmdline``. This does NOT hide the token from *this
    launcher's* own ``/proc/cmdline`` when it was started as
    ``blueprint-runner start --token <token>`` — only supplying the token via
    ``BLUEPRINT_RUNNER_TOKEN`` keeps it off the launcher's argv as well.

    Runs it as a subprocess so signal handling and exit codes are the runner's
    own (graceful shutdown on SIGTERM/SIGINT is handled there). ``_run`` is
    injectable for tests. Returns the child's exit code.
    """
    env = dict(os.environ)
    env["BLUEPRINT_RUNNER_TOKEN"] = token
    env["BLUEPRINT_ORCHESTRATOR_URL"] = url
    argv = [sys.executable, _LOCAL_RUNNER]
    if check:
        argv.append("--check")
    completed = _run(argv, env=env)
    return int(getattr(completed, "returncode", 0) or 0)


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

    if args.command == "check":
        # Full preflight: prereqs above + local_runner's orchestrator/token/isolation
        # report. Non-zero if any prerequisite is missing OR local_runner --check fails.
        print(flush=True)
        rc = run_local_runner(token, url, check=True)
        return 0 if (rc == 0 and not missing) else 1

    # start
    if missing:
        print(
            "\nCannot start: install the missing prerequisite(s) listed above, then re-run.",
            file=sys.stderr,
        )
        return 1
    print(flush=True)
    return run_local_runner(token, url, check=False)


if __name__ == "__main__":
    sys.exit(main())
