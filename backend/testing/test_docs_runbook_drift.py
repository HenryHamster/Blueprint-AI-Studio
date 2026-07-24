"""Drift guard: docs/RUNNER_RUNBOOK.md and docs/runner-install.md stay in sync
with RunnerPreset definitions and the shipped blueprint-runner CLI.

``RunnerPreset`` in ``backend/app/routers/admin_runners.py`` is a ``Literal``
of valid preset names. This file ensures the runbook covers every preset and
contains no references to the excised strategic-resident subsystem — two failure modes
that are easy to miss during a docs-only PR.

``FIRST_LAUNCH_COMMANDS`` and ``MIN_NODE_MAJOR`` in ``scripts/blueprint_runner.py``
are authoritative for what the CLI ships.  ``test_first_launch_matches_shipped_cli``
ensures the runbook documents each first-launch command and the correct Node.js
minimum version so that runbook-only edits cannot silently diverge from the
launcher source.

``test_runner_install_first_launch_matches_shipped_cli`` applies the same guard
to ``docs/runner-install.md`` so that its Section 2 first-launch instructions
stay aligned with ``FIRST_LAUNCH_COMMANDS`` and do not revert to the legacy
``python3 scripts/local_runner.py`` invocation.
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
_RUNNER_INSTALL_PATH = _REPO_ROOT / "docs" / "runner-install.md"
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


def _extract_pinned_codex_version() -> str:
    """Parse PINNED_CODEX_VERSION from scripts/blueprint_runner.py without importing it."""
    source = _BLUEPRINT_RUNNER.read_text()
    m = re.search(r'^PINNED_CODEX_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if not m:
        raise AssertionError("PINNED_CODEX_VERSION not found in scripts/blueprint_runner.py")
    return m.group(1)


def test_codex_version_in_runbook() -> None:
    """The runbook's Prerequisites table must document the pinned Codex CLI version.

    Reads ``PINNED_CODEX_VERSION`` from ``scripts/blueprint_runner.py`` (the
    source of truth for which ``@openai/codex`` version is required) and asserts
    the runbook's Prerequisites table mentions that exact version string, so an
    operator following the runbook installs a compatible Codex CLI.

    This test intentionally parses the source rather than importing it so the
    backend test suite does not depend on the scripts/ Python path.
    """
    runbook = _RUNBOOK_PATH.read_text()
    codex_version = _extract_pinned_codex_version()

    assert codex_version in runbook, (
        f"PINNED_CODEX_VERSION={codex_version!r} in scripts/blueprint_runner.py "
        f"but docs/RUNNER_RUNBOOK.md does not mention '{codex_version}' — "
        "update the Prerequisites table in the runbook to document the pinned "
        "Codex CLI version so operators install a compatible version."
    )


def test_all_preset_names_appear_in_runbook() -> None:
    """Every RunnerPreset name appears in the runbook."""
    text = _RUNBOOK_PATH.read_text()
    for name in _PRESET_NAMES:
        assert f'"{name}"' in text, (
            f'RunnerPreset "{name}" not found in docs/RUNNER_RUNBOOK.md'
        )


def test_no_resident_references() -> None:
    """The runbook must not reference the excised strategic-resident subsystem.

    The conversational Workday resident was restored (#16075) as a protected
    surface pinned to the local runner, so the word 'resident' alone is no
    longer banned.  The specific patterns that were excised — the
    ``strategic_resident`` module name and the ``StrategicResident`` class —
    remain prohibited so that any accidental re-introduction of the old
    subsystem is caught immediately.
    """
    text = _RUNBOOK_PATH.read_text()
    for banned in ("strategic_resident", "StrategicResident"):
        assert banned not in text, (
            f"docs/RUNNER_RUNBOOK.md references '{banned}' "
            f"(strategic-resident subsystem was removed — use the conversational "
            f"Workday resident API instead)"
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


def test_runner_install_first_launch_matches_shipped_cli() -> None:
    """docs/runner-install.md Section 2 must use the shipped blueprint-runner CLI.

    Reads ``FIRST_LAUNCH_COMMANDS`` from ``scripts/blueprint_runner.py`` (the
    source of truth for first-launch subcommands) and asserts that:

    1. Each first-launch subcommand appears as ``blueprint-runner <cmd>`` in
       ``docs/runner-install.md``, so a renamed or removed subcommand surfaces
       immediately.
    2. The legacy ``python3 scripts/local_runner.py`` invocation does not appear
       in Section 2 (## 2. First launch), preventing regression to the old path
       that predated the ``blueprint-runner`` CLI wrapper.

    This test intentionally parses the source rather than importing it so
    the backend test suite does not depend on the scripts/ Python path.
    """
    import re as _re

    doc = _RUNNER_INSTALL_PATH.read_text()
    commands = _extract_first_launch_commands()

    for cmd in commands:
        assert f"blueprint-runner {cmd}" in doc, (
            f"'blueprint-runner {cmd}' not found in docs/runner-install.md — "
            f"update the install doc or remove '{cmd}' from FIRST_LAUNCH_COMMANDS "
            f"in scripts/blueprint_runner.py"
        )

    # Extract Section 2 to scope the legacy-invocation check.
    section2_match = _re.search(
        r"##\s*2\.\s*First launch.*?(?=##\s*\d\.|\Z)",
        doc,
        _re.IGNORECASE | _re.DOTALL,
    )
    section2 = section2_match.group() if section2_match else doc
    assert "python3 scripts/local_runner.py" not in section2, (
        "docs/runner-install.md Section 2 still references the legacy "
        "'python3 scripts/local_runner.py' invocation. "
        "Use 'blueprint-runner start' instead — the CLI wrapper is the "
        "canonical first-launch path."
    )
