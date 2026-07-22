"""Drift guard: docs/RUNNER_RUNBOOK.md stays in sync with RunnerPreset definitions
and the shipped blueprint-runner CLI.

``RunnerPreset`` in ``backend/app/routers/admin_runners.py`` is a ``Literal``
of valid preset names. This file ensures the runbook covers every preset and
contains no references to the (removed) resident subsystem — two failure modes
that are easy to miss during a docs-only PR.

``FIRST_LAUNCH_COMMANDS`` and ``MIN_NODE_MAJOR`` in ``scripts/blueprint_runner.py``
are authoritative for what the CLI ships.  ``test_first_launch_matches_shipped_cli``
ensures the runbook documents each first-launch command and the correct Node.js
minimum version so that runbook-only edits cannot silently diverge from the
launcher source.
"""

from __future__ import annotations

import re
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_ROOT = _THIS_DIR.parent
_REPO_ROOT = _BACKEND_ROOT.parent

# Inside the Docker test harness the backend source is mounted at /app while
# the full repo is bind-mounted at /repo (see scripts/test-backend.sh).  When
# running locally the layout is the natural checkout tree.  Fall back to /repo
# when the computed root does not contain the expected docs directory.
if not (_REPO_ROOT / "docs").is_dir() and (Path("/repo") / "docs").is_dir():
    _REPO_ROOT = Path("/repo")

_RUNBOOK_PATH = _REPO_ROOT / "docs" / "RUNNER_RUNBOOK.md"
_BLUEPRINT_RUNNER = _REPO_ROOT / "scripts" / "blueprint_runner.py"

# RunnerPreset = Literal["default", "strategic", "strategic_local"]
# in backend/app/routers/admin_runners.py:122
_PRESET_NAMES = ("default", "strategic", "strategic_local")


def _extract_first_launch_commands() -> tuple[str, ...]:
    """Parse FIRST_LAUNCH_COMMANDS from scripts/blueprint_runner.py without importing it."""
    source = _BLUEPRINT_RUNNER.read_text()
    m = re.search(r'FIRST_LAUNCH_COMMANDS\s*:\s*tuple\[.*?\]\s*=\s*\(([^)]+)\)', source)
    if not m:
        raise AssertionError(
            "FIRST_LAUNCH_COMMANDS not found in scripts/blueprint_runner.py"
        )
    return tuple(s.strip().strip('"').strip("'") for s in m.group(1).split(",") if s.strip())


def _extract_min_node_major() -> int:
    """Parse MIN_NODE_MAJOR from scripts/blueprint_runner.py without importing it."""
    source = _BLUEPRINT_RUNNER.read_text()
    m = re.search(r'^MIN_NODE_MAJOR\s*=\s*(\d+)', source, re.MULTILINE)
    if not m:
        raise AssertionError("MIN_NODE_MAJOR not found in scripts/blueprint_runner.py")
    return int(m.group(1))


def test_all_preset_names_appear_in_runbook() -> None:
    """Every RunnerPreset name appears in the runbook."""
    text = _RUNBOOK_PATH.read_text()
    for name in _PRESET_NAMES:
        assert f'"{name}"' in text, (
            f'RunnerPreset "{name}" not found in docs/RUNNER_RUNBOOK.md'
        )


def test_no_resident_references() -> None:
    """The runbook must not reference the excised resident subsystem."""
    text = _RUNBOOK_PATH.read_text().lower()
    assert "resident" not in text, (
        "docs/RUNNER_RUNBOOK.md still references 'resident' "
        "(strategic-resident subsystem was removed)"
    )


def test_first_launch_matches_shipped_cli() -> None:
    """The runbook's first-launch commands and Node.js minimum match the shipped CLI.

    Reads ``FIRST_LAUNCH_COMMANDS`` and ``MIN_NODE_MAJOR`` directly from
    ``scripts/blueprint_runner.py`` (the source of truth) and asserts that:

    1. Each first-launch subcommand appears as ``blueprint-runner <cmd>`` in
       the runbook, so a renamed or removed subcommand fails immediately.
    2. The minimum Node.js major version documented in the runbook's
       Prerequisites table matches the constant in the launcher script.

    This test intentionally parses the source rather than importing it so
    the backend test suite does not depend on the scripts/ Python path.
    """
    runbook = _RUNBOOK_PATH.read_text()
    commands = _extract_first_launch_commands()
    node_major = _extract_min_node_major()

    for cmd in commands:
        assert f"blueprint-runner {cmd}" in runbook, (
            f"'blueprint-runner {cmd}' not found in docs/RUNNER_RUNBOOK.md — "
            f"update the runbook or remove '{cmd}' from FIRST_LAUNCH_COMMANDS "
            f"in scripts/blueprint_runner.py"
        )

    # The Prerequisites table must reference the same major version as MIN_NODE_MAJOR.
    assert f"v{node_major}" in runbook or f">= {node_major}" in runbook, (
        f"MIN_NODE_MAJOR={node_major} in scripts/blueprint_runner.py but "
        f"docs/RUNNER_RUNBOOK.md does not mention 'v{node_major}' or '>= {node_major}' — "
        "update the runbook's Prerequisites table to match."
    )
