"""Tests for the ROT-vs-ADDITION classifier.

Every drift assertion here is made against a REAL fixture tree with REAL rot or
a REAL addition planted in it -- a file deleted, a test function removed, a test
file added -- and read back through the same code path the guard-data lane
calls. Nothing is mocked: a classifier that agrees with a stub about a stub is
the exact shape of green test that certifies nothing.

The two properties that matter:

* planted ROT is reported as ROT, with the offending entry named. This is the
  half that must keep blocking; a classifier that softened it would be worse
  than no classifier at all.
* a planted ADDITION is reported as ADDITION and NOT as rot, so the routine case
  stops reading as a defect.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from derived_artifact_classify import (  # noqa: E402
    ADDITION,
    DECISION_BEARING,
    DIRECTIONAL,
    FRESH,
    MIXED,
    NOT_DIRECTIONAL,
    ROT,
    classify,
    classify_sentinel,
    format_unclassified,
    ranked_queue_live,
)
from static_release_guard import SENTINEL_REPAIR_COMMANDS  # noqa: E402

DURATIONS_SENTINEL = "backend/testing/test_test_durations_freshness.py"
VITEST_SENTINEL = "backend/testing/test_vitest_durations_freshness.py"
QUEUE_SENTINEL = "backend/testing/test_test_estate_ranked_queue.py"

# Stdlib-only helpers the classifier loads out of the tree it is pointed at.
FIXTURE_SUPPORT = (
    "backend/testing/estate_inventory.py",
    "backend/testing/identity_extraction.py",
    "backend/testing/durations_measurability.py",
)

_PYPROJECT = """[tool.pytest.ini_options]
addopts = "-m 'not integration'"
"""


def _fixture_repo(root: Path) -> Path:
    """A real tree the classifier can read both sides of every artifact from."""
    for relative in FIXTURE_SUPPORT:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)

    (root / "backend" / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    testing = root / "backend" / "testing"
    testing.mkdir(parents=True, exist_ok=True)
    (testing / "test_alpha.py").write_text(
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n",
        encoding="utf-8",
    )
    (testing / "test_beta.py").write_text(
        "def test_three():\n    assert True\n", encoding="utf-8"
    )
    _write_backend_durations(
        root,
        {
            "testing/test_alpha.py::test_one": 1.5,
            "testing/test_alpha.py::test_two": 2.5,
            "testing/test_beta.py::test_three": 0.5,
        },
    )

    for suite in ("admin-ui", "agent"):
        src = root / suite / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "example.test.ts").write_text(
            'it("renders", () => expect(true).toBe(true));\n', encoding="utf-8"
        )
        _write_vitest_durations(
            root,
            suite,
            [{"file": "src/example.test.ts", "duration_ms": 125, "measured": True}],
        )

    _write_ranked_queue(root, FIXTURE_QUEUE_ROWS)
    return root


# Everything the ranked queue indexes in the fixture tree: backend modules plus
# every Vitest file across the three collected suites.
FIXTURE_QUEUE_ROWS = (
    "admin-ui/src/example.test.ts",
    "agent/src/example.test.ts",
    "backend/testing/test_alpha.py",
    "backend/testing/test_beta.py",
)


def _write_backend_durations(root: Path, data: dict) -> None:
    (root / "backend" / ".test_durations").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline=""
    )


def _write_vitest_durations(root: Path, suite: str, files: list[dict]) -> None:
    (root / suite / ".test_durations.json").write_text(
        json.dumps({"files": files}, indent=2) + "\n", encoding="utf-8", newline=""
    )


def _write_ranked_queue(root: Path, paths: tuple[str, ...]) -> None:
    """Render only what the guard's row regex reads: two cols, then the path.

    Mirrors the real rendering, which carries no rank ordinal: that column was
    removed because it renumbered every row below an insertion, which made the
    artifact conflict with every concurrent branch.
    """
    rows = "\n".join(f"| P2 | backend | `{path}` | 120 |" for path in paths)
    (root / "test-estate-ranked-queue.md").write_text(
        "# queue\n\n| pri | area | path | lines |\n|---|---|---|---|\n" + rows + "\n",
        encoding="utf-8",
        newline="",
    )


def _only(results: list, artifact: str):
    matches = [result for result in results if result.artifact == artifact]
    assert len(matches) == 1, f"{artifact} not classified exactly once: {results}"
    return matches[0]


# ---------------------------------------------------------------------------
# backend/.test_durations
# ---------------------------------------------------------------------------


def test_a_clean_fixture_tree_is_fresh_in_both_directions(tmp_path: Path) -> None:
    """The floor: no drift planted, nothing reported. Otherwise every other
    assertion below could be passing on background noise."""

    repo = _fixture_repo(tmp_path)

    for result in classify(repo):
        assert result.verdict == FRESH, (result.artifact, result.rot, result.additions)


def test_a_deleted_test_file_is_reported_as_rot(tmp_path: Path) -> None:
    """The hole this whole change closes: delete the module, keep the rows.

    Nothing in the repository failed on this shape before -- _stale_entries
    skips a key whose file is gone, and the coverage guard only looks for
    modules with no entry.
    """

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_beta.py").unlink()

    result = _only(
        classify_sentinel(repo, DURATIONS_SENTINEL), "backend/.test_durations"
    )

    assert result.verdict == ROT
    assert result.rot == ("testing/test_beta.py::test_three",)
    assert result.additions == ()
    assert "prune_stale_durations" in result.rot_repair


def test_a_deleted_test_function_is_reported_as_rot(tmp_path: Path) -> None:
    """Node-level rot: the file survives, the test inside it does not."""

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_alpha.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8"
    )

    result = _only(
        classify_sentinel(repo, DURATIONS_SENTINEL), "backend/.test_durations"
    )

    assert result.verdict == ROT
    assert result.rot == ("testing/test_alpha.py::test_two",)


def test_a_new_test_file_is_reported_as_addition_and_never_as_rot(
    tmp_path: Path,
) -> None:
    """The routine case, and the one that used to red identically to rot.

    It must also carry the honest price: a missing duration is a MEASUREMENT,
    so the repair is a timed run and not a command that could be run here.
    """

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_gamma.py").write_text(
        "def test_four():\n    assert True\n", encoding="utf-8"
    )

    result = _only(
        classify_sentinel(repo, DURATIONS_SENTINEL), "backend/.test_durations"
    )

    assert result.verdict == ADDITION
    assert result.rot == ()
    assert result.additions == ("testing/test_gamma.py",)
    assert result.addition_disposition == "measurement"
    assert "store-backend-durations" in result.addition_repair


def test_rot_and_an_addition_together_are_reported_as_mixed(tmp_path: Path) -> None:
    """A rename is both at once, and the rot half must not be swallowed."""

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_beta.py").rename(
        repo / "backend" / "testing" / "test_beta_renamed.py"
    )

    result = _only(
        classify_sentinel(repo, DURATIONS_SENTINEL), "backend/.test_durations"
    )

    assert result.verdict == MIXED
    assert result.rot == ("testing/test_beta.py::test_three",)
    assert result.additions == ("testing/test_beta_renamed.py",)


def test_a_module_that_cannot_be_timed_is_not_reported_as_rot(tmp_path: Path) -> None:
    """A file that exists but is unmeasurable keeps its historical rows.

    backend/testing/test_invariants.py is this shape in the real repository: it
    declares itself an intentional-zero-tests helper yet carries duration
    entries. Measuring rot against the MEASURABLE set instead of the on-disk set
    reported it as stale and would have sent a reader to delete real data.
    """

    repo = _fixture_repo(tmp_path)
    beta = repo / "backend" / "testing" / "test_beta.py"
    beta.write_text(
        "# blueprint-test-module: intentional-zero-tests -- helpers only\n"
        + beta.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = _only(
        classify_sentinel(repo, DURATIONS_SENTINEL), "backend/.test_durations"
    )

    assert result.verdict == FRESH, result.rot
    # ...and it is equally not demanded as a missing addition.
    assert result.additions == ()


# ---------------------------------------------------------------------------
# vitest artifacts
# ---------------------------------------------------------------------------


def test_a_deleted_vitest_file_is_rot_and_a_new_one_is_an_addition(
    tmp_path: Path,
) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / "admin-ui" / "src" / "example.test.ts").unlink()
    (repo / "agent" / "src" / "added.test.ts").write_text(
        'it("x", () => expect(1).toBe(1));\n', encoding="utf-8"
    )

    results = classify_sentinel(repo, VITEST_SENTINEL)

    admin = _only(results, "admin-ui/.test_durations.json")
    assert admin.verdict == ROT
    assert admin.rot == ("src/example.test.ts",)

    agent = _only(results, "agent/.test_durations.json")
    assert agent.verdict == ADDITION
    assert agent.additions == ("src/added.test.ts",)
    assert agent.addition_disposition == "measurement"
    assert "vitest-test-telemetry" in agent.addition_repair


# ---------------------------------------------------------------------------
# test-estate-ranked-queue.md
# ---------------------------------------------------------------------------


def test_the_ranked_queue_reports_both_directions_as_mechanical(
    tmp_path: Path,
) -> None:
    """A pure report: neither direction costs a measurement, so both name a
    command a reader can actually run."""

    repo = _fixture_repo(tmp_path)
    rows = tuple(
        path for path in FIXTURE_QUEUE_ROWS if path != "backend/testing/test_beta.py"
    ) + ("backend/testing/test_vanished.py",)
    _write_ranked_queue(repo, rows)

    result = _only(
        classify_sentinel(repo, QUEUE_SENTINEL), "test-estate-ranked-queue.md"
    )

    assert result.verdict == MIXED
    assert result.rot == ("backend/testing/test_vanished.py",)
    assert result.additions == ("backend/testing/test_beta.py",)
    assert result.rot_disposition == "mechanical"
    assert result.addition_disposition == "mechanical"


# Two full walks of the real test estate (the generator's and the classifier's).
# An inner budget below the real cost of a HEALTHY operation is a flake, not a
# guard: it fails with a timeout instead of an assertion, which reads as an
# infrastructure fault and gets retried rather than fixed. State the real budget
# where the cost is.
#
# Whole file measured 20.1s. 120s is ~6x that, which covers a loaded 2-vCPU CI
# runner reading this tree over a bind mount. It was 240s, a number picked to
# clear the old 30s lane default rather than sized on anything; 240 forced the
# scripts lane to grant this file 300s of budget it cannot use, and a budget
# that advertises a cost the work never incurs hides a real regression.
@pytest.mark.timeout(120)
def test_ranked_queue_live_matches_the_generator() -> None:
    """The classifier's row set is the generator's, against the real tree.

    ranked_queue_live() restates the generator's path derivation rather than
    importing it (see its docstring for why importing is unsafe in-process), so
    this is the pin that stops the restatement from drifting.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.generate_test_estate_ranked_queue import collect_test_files

    expected = {record.path for record in collect_test_files(REPO_ROOT)}

    assert ranked_queue_live(REPO_ROOT) == expected


# ---------------------------------------------------------------------------
# Failure handling and the registry ratchet
# ---------------------------------------------------------------------------


def test_an_unreadable_artifact_is_reported_not_raised(tmp_path: Path) -> None:
    """This runs AFTER a failure that already blocks. A traceback here would
    replace the guard's precise message with this tool's stack."""

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / ".test_durations").write_text("{not json", encoding="utf-8")

    result = _only(
        classify_sentinel(repo, DURATIONS_SENTINEL), "backend/.test_durations"
    )

    assert result.verdict == "error"
    assert "JSONDecodeError" in result.error


def test_every_guard_data_sentinel_is_explicitly_placed() -> None:
    """The ratchet: a derived artifact cannot become unclassified by omission.

    ``SENTINEL_REPAIR_COMMANDS`` is the population where "which direction is
    this?" is a live question. Each entry must be answered on purpose --
    classified, or recorded with the reason it is not.
    """
    placed = set(DIRECTIONAL) | set(DECISION_BEARING) | set(NOT_DIRECTIONAL)

    assert placed == set(SENTINEL_REPAIR_COMMANDS), (
        "a sentinel with a known repair command is neither given a classifier "
        "nor recorded as unclassifiable; place it deliberately rather than "
        "leaving a reader to guess whether a red means rot or an addition"
    )
    assert not set(DIRECTIONAL) & set(DECISION_BEARING)
    assert not set(DIRECTIONAL) & set(NOT_DIRECTIONAL)
    assert not set(DECISION_BEARING) & set(NOT_DIRECTIONAL)

    for sentinel, entries in DIRECTIONAL.items():
        assert entries, sentinel
        for entry in entries:
            assert entry.sentinel == sentinel
            assert entry.rot_repair, f"{entry.artifact} names no rot repair"
            assert entry.addition_repair, f"{entry.artifact} names no addition repair"

    for registry in (DECISION_BEARING, NOT_DIRECTIONAL):
        for sentinel, why in registry.items():
            assert len(why) > 120, (
                f"{sentinel} is left unclassified on a one-line rationale; "
                "refusing to classify has to be argued, not asserted"
            )


def test_the_unclassified_explanation_names_every_unclassified_sentinel() -> None:
    """The honest half of the report is not allowed to quietly go missing."""

    text = format_unclassified()

    for sentinel in (*DECISION_BEARING, *NOT_DIRECTIONAL):
        assert sentinel in text


def test_workday_protection_rationale_matches_the_guard_it_describes() -> None:
    """The 'both directions block' claim stays tied to the guard's own proof.

    This entry used to sit in ``NOT_DIRECTIONAL`` describing the manifest's
    ``test_count`` values as FLOORS, with the explicit claim that "recorded <
    live never fails at all because a floor that is exceeded is satisfied".
    That was false. The guard reports ``declared_exact=`` and owns a dedicated
    test proving a declaration BELOW the tree fails -- which is exactly the red
    a PR that ADDS tests to a protected file gets. An agent reading the old
    text when guard-data goes red concludes its own addition cannot be the
    cause and spends a review cycle hunting elsewhere.

    So the rationale is pinned against the guard source, not against itself: if
    someone ever makes the count a real floor by deleting the exactness test,
    this fails and the text has to be rewritten with it.
    """
    sentinel = "backend/testing/test_conversational_workday_protection.py"

    assert sentinel in DECISION_BEARING, (
        "the direction IS computable here and both directions block; recording "
        "it as non-directional told readers the opposite of what the guard does"
    )
    assert sentinel not in NOT_DIRECTIONAL

    guard = (REPO_ROOT / sentinel).read_text(encoding="utf-8")
    assert "declared_exact=" in guard, (
        "the guard no longer reports an EXACT declaration; the rationale in "
        "DECISION_BEARING claims it does and is now stale"
    )
    assert "def test_new_floor_slack_relative_to_integration_branch_fails" in guard, (
        "the test proving that declaring FEWER tests than the tree has still "
        "fails is gone; without it the count really would be a floor and this "
        "rationale would be describing a guard that no longer exists"
    )

    why = DECISION_BEARING[sentinel]
    assert "EXACT" in why
    assert "never fails" not in why, (
        "the rationale is back to claiming slack is satisfied, which the "
        "guard's own test refutes"
    )
