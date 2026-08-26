"""Tests for the offline ROT repair of the durations artifacts.

The pruner exists because rot and additions cost wildly different things on a
measurement artifact, and only the expensive one actually needs a measurement.
Three properties have to hold for that to be safe, and each is asserted here
against a real tree with real drift planted in it:

* it removes what is genuinely stale, and ONLY that;
* it never writes a duration -- a fabricated row is worse than the red it would
  clear, because pytest-split already estimates an unknown test from the mean
  while the fake row permanently marks the file measured;
* it refuses to rewrite a file it cannot re-serialise byte-identically, because
  these artifacts are the repository's worst merge ratchets and a whole-file
  reformat conflicts with every concurrent branch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from derived_artifact_classify import ROT, classify_sentinel  # noqa: E402
from prune_stale_durations import (  # noqa: E402
    main,
    prune_backend_durations,
    prune_vitest_durations,
)
from test_derived_artifact_classify import (  # noqa: E402
    _fixture_repo,
    _write_vitest_durations,
)

DURATIONS_SENTINEL = "backend/testing/test_test_durations_freshness.py"


def _durations(repo: Path) -> dict:
    return json.loads(
        (repo / "backend" / ".test_durations").read_text(encoding="utf-8")
    )


def test_a_deleted_file_and_a_deleted_node_are_both_pruned(tmp_path: Path) -> None:
    """Both rot shapes, in one pass, leaving every live row untouched."""

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_beta.py").unlink()
    (repo / "backend" / "testing" / "test_alpha.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8"
    )

    result = prune_backend_durations(repo, write=True)

    assert result.written
    assert set(result.removed) == {
        "testing/test_alpha.py::test_two",
        "testing/test_beta.py::test_three",
    }
    assert _durations(repo) == {"testing/test_alpha.py::test_one": 1.5}


def test_pruning_actually_clears_the_classifier_rot_verdict(tmp_path: Path) -> None:
    """The repair is proven by the reporter, not by the pruner's own claim."""

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_beta.py").unlink()

    before = classify_sentinel(repo, DURATIONS_SENTINEL)[0]
    assert before.verdict == ROT

    prune_backend_durations(repo, write=True)

    after = classify_sentinel(repo, DURATIONS_SENTINEL)[0]
    assert after.rot == ()


def test_a_missing_measurement_is_never_invented(tmp_path: Path) -> None:
    """The unpayable half stays unpaid. A test with no timing must still block.

    This is the assertion that keeps the pruner from quietly becoming the
    fabricated-placeholder shortcut MEASUREMENT_TAX_GUIDANCE warns against.
    """

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_gamma.py").write_text(
        "def test_four():\n    assert True\n", encoding="utf-8"
    )
    before = (repo / "backend" / ".test_durations").read_bytes()

    result = prune_backend_durations(repo, write=True)

    assert result.removed == ()
    assert not result.written
    assert (repo / "backend" / ".test_durations").read_bytes() == before
    assert "testing/test_gamma.py" not in json.dumps(_durations(repo))


def test_report_mode_changes_nothing_and_exits_nonzero(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_beta.py").unlink()
    before = (repo / "backend" / ".test_durations").read_bytes()

    status = main(["--repo-root", str(repo)])

    assert status == 1
    assert (repo / "backend" / ".test_durations").read_bytes() == before


def test_a_clean_tree_reports_clean_and_exits_zero(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)

    assert main(["--repo-root", str(repo)]) == 0
    assert main(["--repo-root", str(repo), "--write"]) == 0


def test_a_file_that_does_not_round_trip_is_refused_not_reformatted(
    tmp_path: Path,
) -> None:
    """Fail closed on an unfamiliar shape rather than rewriting 21k lines.

    A whole-file reformat of backend/.test_durations conflicts with every
    concurrent branch, so an unrecognised serialisation must stop the pruner
    dead -- and it must stop it with a REFUSAL, not a silent skip.
    """

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_beta.py").unlink()
    # Same data, compact shape: parses fine, re-serialises differently.
    original = json.dumps(_durations(repo))
    (repo / "backend" / ".test_durations").write_text(
        original, encoding="utf-8", newline=""
    )

    result = prune_backend_durations(repo, write=True)

    assert result.blocked
    assert "round-trip" in result.refused
    assert not result.written
    assert (repo / "backend" / ".test_durations").read_text(
        encoding="utf-8"
    ) == original
    assert main(["--repo-root", str(repo), "--write"]) == 2


def test_a_prune_writes_lf_only_bytes(tmp_path: Path) -> None:
    """The Windows trap: a CRLF rewrite is a whole-file diff on every row."""

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / "testing" / "test_beta.py").unlink()

    prune_backend_durations(repo, write=True)

    assert b"\r" not in (repo / "backend" / ".test_durations").read_bytes()


def test_vitest_rot_is_pruned_and_other_suites_are_untouched(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    _write_vitest_durations(
        repo,
        "admin-ui",
        [
            {"file": "src/example.test.ts", "duration_ms": 125, "measured": True},
            {"file": "src/vanished.test.ts", "duration_ms": 40, "measured": True},
        ],
    )
    agent_before = (repo / "agent" / ".test_durations.json").read_bytes()

    result = prune_vitest_durations(repo, "admin-ui", write=True)

    assert result.removed == ("src/vanished.test.ts",)
    data = json.loads((repo / "admin-ui" / ".test_durations.json").read_text("utf-8"))
    assert [entry["file"] for entry in data["files"]] == ["src/example.test.ts"]
    assert (repo / "agent" / ".test_durations.json").read_bytes() == agent_before


def test_an_absent_artifact_is_not_an_error(tmp_path: Path) -> None:
    """A fixture or partial checkout without the artifact must not crash the
    lane that calls this after a failure."""

    repo = _fixture_repo(tmp_path)
    (repo / "backend" / ".test_durations").unlink()

    result = prune_backend_durations(repo, write=True)

    assert not result.blocked
    assert result.removed == ()


# One pass over the real 21k-row artifact and the whole backend test roster.
# The standalone lane runs with --timeout=30; an inner budget below the honest
# cost of a healthy run fails as a TIMEOUT rather than an assertion, which reads
# as infrastructure and gets retried instead of fixed.
@pytest.mark.timeout(240)
def test_the_real_repository_artifacts_are_prunable_and_carry_no_rot() -> None:
    """Two properties on the real tree, in one pass because the walk is not cheap.

    The refusal path must not be silently ON. If any of the three artifacts
    stopped matching this script's serialiser, every prune would refuse and the
    rot-only repair would quietly become a no-op -- a guard that cannot fix
    anything, reporting clean.

    And the tree this lands on must already be clean, so the new orphan guard
    cannot red on arrival.
    """

    for result in (
        prune_backend_durations(REPO_ROOT, write=False),
        prune_vitest_durations(REPO_ROOT, "admin-ui", write=False),
        prune_vitest_durations(REPO_ROOT, "agent", write=False),
    ):
        assert not result.blocked, result.refused
        assert result.removed == (), (
            f"{result.artifact} carries rot on the branch this lands on: "
            f"{result.removed[:10]}"
        )
