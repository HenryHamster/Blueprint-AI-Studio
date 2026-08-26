"""Tests for the guard-data "Derived artifacts" lane.

The lane exists so ``.github/workflows/guard-data.yml`` -- an operator-only
surface -- holds one line instead of a base64 roster and a hand-maintained
markdown table that had already drifted out of date. Moving behaviour out of
YAML is only an improvement if the behaviour is then actually tested, so the
three properties that matter are pinned by execution, not by reading the file:

* the roster reaches the harness intact, in the shape it decodes;
* a missing or empty roster fails CLOSED, because a roster that silently
  resolves to nothing is a check that runs no tests and reports success;
* the classification is ADVISORY -- it can never change the exit code. A gate
  that can talk itself green is not a gate.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
# scripts/*.py tests run with --noconftest, so the shared offender-identity
# producer is reached by the same explicit path insertion the other scripts
# guards use rather than by a conftest-installed sys.path entry.
sys.path.insert(0, str(REPO_ROOT / "backend"))

from derived_artifact_classify import (  # noqa: E402
    DECISION_BEARING,
    DIRECTIONAL,
    NOT_DIRECTIONAL,
)
from testing.fixgate_attribution import format_offender_marker  # noqa: E402

LANE = REPO_ROOT / "scripts" / "guard_data_lane.sh"
ROSTER = REPO_ROOT / "scripts" / "guard_data_selectors.txt"

BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="no bash on this host")

_HARNESS_STUB = """#!/usr/bin/env bash
here="$(cd "$(dirname "$0")/.." && pwd)"
printf '%s' "$BLUEPRINT_STATIC_GUARD_SELECTORS" > "$here/selectors.b64"
printf '%s\\n' "$*" > "$here/harness-args.txt"
echo "STUB HARNESS FAILURE"
exit {harness_exit}
"""

_CLASSIFIER_STUB = """import sys

print("CLASSIFIER RAN", sys.argv[1:])
sys.exit({classifier_exit})
"""


def _selectors(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.replace("\r\n", "\n").split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]


def _lane_repo(
    tmp_path: Path,
    *,
    harness_exit: int = 1,
    classifier_exit: int = 0,
    roster: str | None = None,
) -> Path:
    """A tree with the real lane and stubs at both process boundaries."""

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LANE, repo / "scripts" / "guard_data_lane.sh")
    if roster is None:
        shutil.copy2(ROSTER, repo / "scripts" / "guard_data_selectors.txt")
    else:
        (repo / "scripts" / "guard_data_selectors.txt").write_text(
            roster, encoding="utf-8", newline=""
        )

    # The harness stub records the environment it was handed, so the roster
    # hand-off is asserted on what test-backend.sh would actually have seen.
    (repo / "scripts" / "test-backend.sh").write_text(
        _HARNESS_STUB.format(harness_exit=harness_exit), encoding="utf-8", newline=""
    )
    (repo / "scripts" / "derived_artifact_classify.py").write_text(
        _CLASSIFIER_STUB.format(classifier_exit=classifier_exit),
        encoding="utf-8",
        newline="",
    )
    return repo


def _run(repo: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GITHUB_STEP_SUMMARY": "", **env_extra}
    return subprocess.run(
        [BASH, str(repo / "scripts" / "guard_data_lane.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        # Both process boundaries the lane crosses are stubs here (_HARNESS_STUB
        # echoes and exits; _CLASSIFIER_STUB is three lines of python), so this
        # is a millisecond run. The old 300s advertised a cost this test cannot
        # incur and broke the 30s inner-operation ceiling for no benefit.
        timeout=20,
    )


@requires_bash
def test_the_lane_is_syntactically_valid_bash() -> None:
    # `bash -n` parses one script and exits without running it.
    result = subprocess.run(
        [BASH, "-n", str(LANE)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


@requires_bash
@pytest.mark.parametrize("harness_exit", [0, 1, 2, 7])
def test_the_lane_exits_with_the_harness_status_and_nothing_else(
    tmp_path: Path, harness_exit: int
) -> None:
    """Including the non-1 codes: pytest's usage/interrupt exits must survive."""

    repo = _lane_repo(tmp_path, harness_exit=harness_exit)
    result = _run(repo)
    assert result.returncode == harness_exit, result.stdout + result.stderr


@requires_bash
def test_a_clean_run_does_not_pay_for_classification(tmp_path: Path) -> None:
    repo = _lane_repo(tmp_path, harness_exit=0)
    result = _run(repo)
    assert result.returncode == 0
    assert "CLASSIFIER RAN" not in result.stdout


@requires_bash
def test_the_classification_cannot_turn_a_red_into_a_green(tmp_path: Path) -> None:
    """The advisory lane runs, reports, and leaves the verdict untouched."""

    repo = _lane_repo(tmp_path, harness_exit=1, classifier_exit=0)
    result = _run(repo)
    assert "CLASSIFIER RAN" in result.stdout
    assert result.returncode == 1, "an advisory annotator changed the verdict"


@requires_bash
def test_a_classifier_that_cannot_run_does_not_change_the_verdict_either(
    tmp_path: Path,
) -> None:
    """Fail-open on the ANNOTATION, never on the gate.

    A classifier that crashes must not red a green run and must not green a red
    one; it may only degrade to saying the pytest failure is authoritative.
    """

    repo = _lane_repo(tmp_path, harness_exit=1, classifier_exit=3)
    result = _run(repo)
    assert result.returncode == 1
    assert "authoritative" in (result.stdout + result.stderr)


@requires_bash
def test_the_roster_reaches_the_harness_base64_encoded_and_intact(
    tmp_path: Path,
) -> None:
    """test-backend.sh decodes this env var wholesale; a mangled roster silently
    selects a different (possibly empty) set of tests."""

    repo = _lane_repo(tmp_path, harness_exit=1)
    _run(repo)

    handed = (repo / "selectors.b64").read_text(encoding="utf-8")
    assert handed and not any(ch.isspace() for ch in handed)
    decoded = base64.b64decode(handed).decode("utf-8")
    assert _selectors(decoded) == _selectors(ROSTER.read_text(encoding="utf-8"))

    args = (repo / "harness-args.txt").read_text(encoding="utf-8").split()
    assert args == ["--standalone", "--no-postgres"], (
        "the lane must keep running the standalone, DB-free harness route; "
        "these guards need no database and the check budget assumes it"
    )


@requires_bash
@pytest.mark.parametrize(
    "roster, why",
    [
        (None, "missing"),
        ("", "empty"),
        ("# only a comment\n\n   \n", "comments only"),
    ],
)
def test_a_roster_that_selects_nothing_fails_closed(
    tmp_path: Path, roster: str | None, why: str
) -> None:
    """A roster resolving to nothing must ABORT, not run the harness.

    ``BLUEPRINT_STATIC_GUARD_SELECTORS`` is a wholesale override. Handed an
    empty value the harness falls back to the full standalone roster (slow, and
    Postgres-on); handed a value that decodes to nothing it would select nothing
    and report success. Both are worse than stopping.
    """

    repo = _lane_repo(tmp_path, harness_exit=1, roster=roster or "")
    if roster is None:
        (repo / "scripts" / "guard_data_selectors.txt").unlink()

    result = _run(repo)

    assert result.returncode == 2, why
    assert not (repo / "harness-args.txt").exists(), (
        f"the harness ran with a {why} roster"
    )


@requires_bash
def test_the_step_summary_names_the_right_command_for_each_artifact(
    tmp_path: Path,
) -> None:
    """The table this replaces sent vitest failures to the wrong generator.

    guard-data.yml's "Explain a failure" step mapped both
    ``*/.test_durations.json`` files to ``scripts/batch-store-durations.sh``,
    which regenerates ``backend/.test_durations`` instead. Anyone who followed
    it got a no-op and an unchanged red.
    """

    repo = _lane_repo(tmp_path, harness_exit=1)
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8", newline="")
    _run(repo, GITHUB_STEP_SUMMARY=str(summary))

    text = summary.read_text(encoding="utf-8")
    assert "vitest-test-telemetry.mjs --suite admin-ui" in text
    assert "vitest-test-telemetry.mjs --suite agent" in text
    assert "batch-store-durations.sh" not in text, (
        "the vitest artifacts are back on the wrong regenerate command"
    )
    assert "prune_stale_durations.py --write" in text
    assert "store-backend-durations.sh" in text
    assert "Test erosion" in text


def test_every_roster_selector_names_a_file_that_exists() -> None:
    """A selector for a renamed file silently drops that guard from the check.

    The harness resolves selectors relative to ``backend/``, and pytest does not
    fail a run because one of several selectors matched nothing when the others
    did -- so this is exactly the shape of a guard that stops running while the
    check keeps reporting green.
    """

    selectors = _selectors(ROSTER.read_text(encoding="utf-8"))
    assert selectors, "the roster is empty"
    assert len(selectors) == len(set(selectors)), "duplicate selector"

    for selector in selectors:
        relative = selector.split("::", 1)[0]
        path = REPO_ROOT / "backend" / relative
        assert path.is_file(), f"{selector} names a file that does not exist"

    for selector in selectors:
        if "::" not in selector:
            continue
        relative, node = selector.split("::", 1)
        source = (REPO_ROOT / "backend" / relative).read_text(encoding="utf-8")
        assert f"def {node}(" in source, (
            f"{selector} names a test that no longer exists"
        )


def test_every_classified_derived_artifact_guard_is_on_the_roster() -> None:
    """The parity ratchet between what is classified and what actually runs.

    ``derived_artifact_classify`` answers rot-vs-addition for a fixed set of
    guards. If one of them is not in this roster the check never runs it, so the
    classification would be describing a failure that this lane cannot produce.
    """

    classified = set(DIRECTIONAL) | set(DECISION_BEARING) | set(NOT_DIRECTIONAL)
    on_roster = {
        f"backend/{selector.split('::', 1)[0]}"
        for selector in _selectors(ROSTER.read_text(encoding="utf-8"))
    }

    missing = sorted(classified - on_roster)
    assert not missing, (
        f"classified but never run by the guard-data lane: {missing}; add the "
        "selector to scripts/guard_data_selectors.txt or stop classifying it"
        + "\n"
        + format_offender_marker(missing)
    )


def test_every_roster_selector_is_accounted_for_in_one_direction_or_the_other() -> None:
    """The converse ratchet: the roster cannot grow an unexplained derived guard.

    ``static_release_guard.SENTINEL_REPAIR_COMMANDS`` is this repository's own
    definition of "a guard over a generated artifact". Every roster selector
    must therefore be either classified/explained by
    ``derived_artifact_classify``, or outside that population -- shard balance,
    the impacted-test resolver, the migration chain and the promotion metric
    manifest are behaviour guards over live code, not index files, so they have
    no rot/addition axis and none is invented for them.

    Without this, adding a new derived-artifact guard to the roster would give
    the lane a red it cannot explain, and the omission would be invisible.
    """

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from static_release_guard import SENTINEL_REPAIR_COMMANDS

    classified = set(DIRECTIONAL) | set(DECISION_BEARING) | set(NOT_DIRECTIONAL)
    on_roster = {
        f"backend/{selector.split('::', 1)[0]}"
        for selector in _selectors(ROSTER.read_text(encoding="utf-8"))
    }

    unexplained = sorted(
        (on_roster & set(SENTINEL_REPAIR_COMMANDS)) - classified
    )
    assert not unexplained, (
        f"{unexplained} guard a generated artifact and run in this lane, but "
        "derived_artifact_classify neither classifies them nor records why "
        "not; a reader of their red gets no rot-vs-addition answer"
        + "\n"
        + format_offender_marker(unexplained)
    )
