"""End-to-end tests for the derived-artifact repair pass.

``scripts/sentinel_freshness.py`` already DETECTED every registered derived
artifact that had gone stale and printed its regeneration command. Nothing ever
RAN that command, so the repair happened only when a human noticed -- three
hand-written repair commits in three days (``e38d74d4e1``, ``084cda2dd7``,
``7e8069cde0``), each cut after a promotion PR had already gone red.

``derived_artifact_repair`` closes that loop for the artifacts that carry no
human decision. Two things must hold for that to be safe, and both are pinned
here against the REAL generator over a real fixture tree rather than a fake:

* the repair must actually repair -- regenerate the artifact so its own guard
  passes, on the byte level, including the LF-only newline shape this
  repository's ``.gitattributes`` requires (the generator runs on a Windows
  developer host, where an unguarded write emits CRLF for all ~1200 rows);
* it must fail CLOSED on every way a generator can fail to deliver -- absent,
  non-zero exit, or exit 0 having written nothing -- because a repair reported
  but not performed is exactly the state that turns a guard into one that
  cannot fail.

The registry test at the bottom is the ratchet: a sentinel with a known
regeneration command must be classified EXPLICITLY as decision-free or
never-auto-repairable, so a future derived artifact cannot join the repair
allowlist by omission.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from derived_artifact_repair import (  # noqa: E402
    AUTOREPAIRABLE,
    MEASUREMENT_TAX_GUIDANCE,
    NEVER_AUTOREPAIR,
    ROT_ONLY_AUTOREPAIR,
    repair_entry,
    repair_sentinels,
)
from static_release_guard import SENTINEL_REPAIR_COMMANDS  # noqa: E402

QUEUE_SENTINEL = "backend/testing/test_test_estate_ranked_queue.py"
QUEUE_ARTIFACT = "backend/testing/test-estate-ranked-queue.md"
GENERATOR = "scripts/generate_test_estate_ranked_queue.py"

# Stdlib-only helpers the generator imports from the repository root it
# resolves out of its own location, so a fixture repo needs them beside it.
GENERATOR_SUPPORT = (
    "backend/testing/estate_inventory.py",
    "backend/testing/identity_extraction.py",
)


def _fixture_repo(root: Path) -> Path:
    """A minimal but REAL repo the ranked-queue generator can run against.

    Deliberately not a mock: the property under test is that running the actual
    generator clears the actual guard, and a fake generator would prove only
    that the plumbing calls something.
    """
    for relative in (GENERATOR, *GENERATOR_SUPPORT):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)

    (root / "backend" / "testing").mkdir(parents=True, exist_ok=True)
    (root / "admin-ui" / "src").mkdir(parents=True, exist_ok=True)
    (root / "agent" / "src").mkdir(parents=True, exist_ok=True)
    (root / "backend" / ".test_durations").write_text("{}", encoding="utf-8")
    for suite in ("admin-ui", "agent"):
        (root / suite / ".test_durations.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": "src/example.test.ts",
                            "duration_ms": 125,
                            "measured": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (root / suite / "src" / "example.test.ts").write_text(
            'it("renders", () => expect(true).toBe(true));\n', encoding="utf-8"
        )
    (root / "backend" / "testing" / "test_example.py").write_text(
        "def test_example():\n    assert 1 == 1\n", encoding="utf-8"
    )
    (root / "backend" / "testing" / "conversational_workday_manifest.json").write_text(
        json.dumps({"protect": []}), encoding="utf-8"
    )
    return root


def _guard_says_fresh(root: Path) -> bool:
    """The generator's own ``--check``: the guard, not a paraphrase of it."""
    result = subprocess.run(
        [sys.executable, str(root / GENERATOR), "--check", "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _stub_generator(root: Path, body: str) -> Path:
    """Replace the copied generator with one whose failure mode is chosen."""
    target = root / GENERATOR
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="")
    return target


def test_a_staled_pure_report_is_regenerated_until_its_own_guard_passes(
    tmp_path: Path,
) -> None:
    """The #17936 shape, end to end: a test file added, the queue left behind."""

    repo = _fixture_repo(tmp_path)
    first = repair_sentinels(repo, [QUEUE_SENTINEL])
    assert first.repaired == (QUEUE_SENTINEL,)
    assert _guard_says_fresh(repo)

    # Exactly what PR #17936 did: add a backend test file and stop there.
    (repo / "backend" / "testing" / "test_filter_malformed_payload.py").write_text(
        "def test_filter():\n    assert 1 == 1\n", encoding="utf-8"
    )
    assert not _guard_says_fresh(repo), "fixture did not actually go stale"

    outcome = repair_sentinels(repo, [QUEUE_SENTINEL])

    assert outcome.repaired == (QUEUE_SENTINEL,)
    assert outcome.failed == {}
    assert outcome.changed_artifacts() == (QUEUE_ARTIFACT,)
    assert "test_filter_malformed_payload.py" in (repo / QUEUE_ARTIFACT).read_text(
        encoding="utf-8"
    )
    assert _guard_says_fresh(repo)


def test_a_repair_writes_lf_only_bytes(tmp_path: Path) -> None:
    """The Windows trap: the repair must not commit a whole-file CRLF diff.

    ``.gitattributes`` pins this artifact to ``eol=lf``. A repair that emitted
    CRLF would rewrite all ~1200 rows and conflict with every concurrent
    branch -- the artifact is already the repo's worst merge ratchet.
    """

    repo = _fixture_repo(tmp_path)
    repair_sentinels(repo, [QUEUE_SENTINEL])

    assert b"\r" not in (repo / QUEUE_ARTIFACT).read_bytes()


def test_a_fresh_artifact_is_reported_as_such_and_not_as_a_repair(
    tmp_path: Path,
) -> None:
    """Idempotence, and the honest report of a failure that is NOT staleness.

    When the generator reproduces the checked-in bytes exactly, the sentinel's
    failure was never drift. Reporting that as ``repaired`` would let the lane
    credit a repair that changed nothing and clear a real defect.
    """

    repo = _fixture_repo(tmp_path)
    repair_sentinels(repo, [QUEUE_SENTINEL])

    outcome = repair_sentinels(repo, [QUEUE_SENTINEL])

    assert outcome.repaired == ()
    assert outcome.already_fresh == (QUEUE_SENTINEL,)
    assert outcome.changed_artifacts() == ()
    assert outcome.ran == (QUEUE_SENTINEL,)


def test_an_unregistered_sentinel_is_skipped_and_nothing_is_run(
    tmp_path: Path,
) -> None:
    """There is no fallback path that could run an unlisted repair command."""

    repo = _fixture_repo(tmp_path)
    ratchet = "backend/testing/test_event_type_invariants.py"
    assert ratchet in NEVER_AUTOREPAIR

    outcome = repair_sentinels(repo, [ratchet])

    assert outcome.skipped == (ratchet,)
    assert outcome.ran == ()
    assert outcome.failed == {}
    assert not (repo / QUEUE_ARTIFACT).exists()


def test_a_missing_generator_fails_closed(tmp_path: Path) -> None:
    """An absent generator is a failure to report, never a silent pass."""

    outcome = repair_sentinels(tmp_path, [QUEUE_SENTINEL])

    assert outcome.ran == ()
    assert "generator not found" in outcome.failed[QUEUE_SENTINEL]


def test_a_generator_that_exits_nonzero_is_not_credited_as_a_repair(
    tmp_path: Path,
) -> None:
    repo = _fixture_repo(tmp_path)
    _stub_generator(
        repo,
        'import sys\nsys.stderr.write("boom\\n")\nsys.exit(2)\n',
    )

    outcome = repair_sentinels(repo, [QUEUE_SENTINEL])

    assert outcome.ran == ()
    assert "exited 2" in outcome.failed[QUEUE_SENTINEL]
    assert "boom" in outcome.failed[QUEUE_SENTINEL]


def test_a_generator_that_exits_zero_without_writing_fails_closed(
    tmp_path: Path,
) -> None:
    """Exit 0 is the generator's claim; the artifact on disk is the evidence."""

    repo = _fixture_repo(tmp_path)
    _stub_generator(repo, "import sys\nsys.exit(0)\n")

    outcome = repair_sentinels(repo, [QUEUE_SENTINEL])

    assert outcome.ran == ()
    assert "left" in outcome.failed[QUEUE_SENTINEL]
    assert QUEUE_ARTIFACT in outcome.failed[QUEUE_SENTINEL]


def test_every_sentinel_with_a_repair_command_is_explicitly_classified() -> None:
    """The ratchet: no artifact joins the repair allowlist by omission.

    ``SENTINEL_REPAIR_COMMANDS`` is the set of guards whose repair is a known
    command -- exactly the population where "just run it automatically" is a
    live question. Each one must be answered on purpose: decision-free and
    repairable, or decision-bearing with the reason written down.
    """

    assert set(AUTOREPAIRABLE) | set(NEVER_AUTOREPAIR) == set(
        SENTINEL_REPAIR_COMMANDS
    ), (
        "a sentinel with a known repair command is neither registered as "
        "auto-repairable nor recorded as never-auto-repairable; classify it "
        "rather than leaving the answer to whichever list is read first"
    )
    assert not set(AUTOREPAIRABLE) & set(NEVER_AUTOREPAIR)

    for sentinel, entry in AUTOREPAIRABLE.items():
        assert (REPO_ROOT / entry.script).is_file(), entry.script
        assert entry.args, f"{sentinel} registers no write-mode argument"
        for artifact in entry.artifacts:
            assert (REPO_ROOT / artifact).is_file(), artifact
        assert len(entry.why) > 80, (
            f"{sentinel} is auto-repairable on a one-line rationale; the "
            "argument for regenerating a guard's own evidence has to be legible"
        )

    for sentinel, why in NEVER_AUTOREPAIR.items():
        assert (REPO_ROOT / sentinel).is_file(), sentinel
        assert len(why) > 80, f"{sentinel} records no reviewable reason"


def test_every_measurement_guard_explains_the_new_file_tax() -> None:
    """The unpayable repairs are the ones that need more than a command.

    Both durations guards fail the moment a test file is ADDED -- the artifact
    records measured timings and a new file has none. That is not drift and it
    is not repairable from a checkout, so their guidance must exist, must be
    attached to a guard that is never auto-repaired, and must name the two
    shortcuts that turn a red gate into a worse outcome.
    """

    assert set(MEASUREMENT_TAX_GUIDANCE) <= set(NEVER_AUTOREPAIR)
    assert not set(MEASUREMENT_TAX_GUIDANCE) & set(AUTOREPAIRABLE)

    for sentinel in (
        "backend/testing/test_test_durations_freshness.py",
        "backend/testing/test_vitest_durations_freshness.py",
    ):
        guidance = MEASUREMENT_TAX_GUIDANCE[sentinel]
        assert len(guidance) > 200, sentinel

    backend = MEASUREMENT_TAX_GUIDANCE[
        "backend/testing/test_test_durations_freshness.py"
    ]
    assert "test erosion" in backend
    assert "hand-writing a duration" in backend


def test_a_rot_only_repair_is_a_third_category_not_a_hole_in_the_second() -> None:
    """``ROT_ONLY_AUTOREPAIR`` must not become a back door into auto-repair.

    The two durations guards are correctly in ``NEVER_AUTOREPAIR``: their
    registered repair is a timed run, and no checkout can invent a measurement.
    The rot-only registry does something strictly narrower -- it deletes rows
    for tests that no longer exist -- so a sentinel listed there has to remain
    listed as never-auto-repairable too. If an entry ever migrated out of
    ``NEVER_AUTOREPAIR`` into this registry, the "never regenerate a
    measurement" rule would be silently repealed by a data edit rather than by
    an argument.
    """

    assert set(ROT_ONLY_AUTOREPAIR) <= set(NEVER_AUTOREPAIR), (
        "a rot-only repair was registered for a sentinel that is no longer "
        "recorded as never-auto-repairable; pruning stale rows does not make "
        "the artifact regenerable from a checkout"
    )
    assert not set(ROT_ONLY_AUTOREPAIR) & set(AUTOREPAIRABLE), (
        "a sentinel is registered in both repair registries; the two passes "
        "run over disjoint sets and a duplicate would repair it twice"
    )
    assert set(ROT_ONLY_AUTOREPAIR) <= set(MEASUREMENT_TAX_GUIDANCE), (
        "rot-only repair exists because the ADDITION half is unpayable; a "
        "sentinel with no measurement-tax guidance has no such asymmetry and "
        "should be repaired normally or not at all"
    )

    for sentinel, entry in ROT_ONLY_AUTOREPAIR.items():
        assert (REPO_ROOT / sentinel).is_file(), sentinel
        assert (REPO_ROOT / entry.script).is_file(), entry.script
        assert entry.args, f"{sentinel} registers no write-mode argument"
        for artifact in entry.artifacts:
            assert (REPO_ROOT / artifact).is_file(), artifact
        assert len(entry.why) > 80, (
            f"{sentinel} claims a rot-only repair on a one-line rationale; the "
            "argument for deleting recorded evidence has to be legible"
        )


def test_repair_entry_resolves_both_registries_and_only_those() -> None:
    """One lookup, so a caller cannot consult the wrong registry by accident."""

    for sentinel in AUTOREPAIRABLE:
        assert repair_entry(sentinel) is AUTOREPAIRABLE[sentinel]
    for sentinel in ROT_ONLY_AUTOREPAIR:
        assert repair_entry(sentinel) is ROT_ONLY_AUTOREPAIR[sentinel]
    # Fail-closed on anything else: a sentinel with no registered repair must
    # raise rather than resolve to a default, so a caller cannot silently run
    # the wrong generator against it.
    for sentinel in set(NEVER_AUTOREPAIR) - set(ROT_ONLY_AUTOREPAIR):
        with pytest.raises(KeyError):
            repair_entry(sentinel)
    with pytest.raises(KeyError):
        repair_entry("backend/testing/test_nothing_at_all.py")


def test_a_rot_only_sentinel_is_not_repaired_by_the_default_registry() -> None:
    """``repair_sentinels`` defaults to ``AUTOREPAIRABLE``.

    A rot-only sentinel passed without its registry must be SKIPPED, not
    repaired -- otherwise the caller's two-pass split would be decorative and a
    single mistaken call would run a prune under the wrong classification.
    """

    for sentinel in ROT_ONLY_AUTOREPAIR:
        outcome = repair_sentinels(REPO_ROOT, [sentinel])
        assert outcome.skipped == (sentinel,)
        assert outcome.ran == ()
        assert outcome.failed == {}
