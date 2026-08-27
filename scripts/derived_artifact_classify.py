#!/usr/bin/env python3
"""Tell derived-artifact ROT apart from a derived-artifact ADDITION.

WHY THIS EXISTS
---------------
``.github/workflows/guard-data.yml`` is the ONLY per-PR gate on the integration
branch (ci.yml's ``pull_request`` trigger is ``branches: [main]``, so automain
PRs carry zero check-runs by design).  Its "Derived artifacts" check runs the
freshness guards and reports one undifferentiated red for two failures that are
not the same thing at all:

* **ROT** -- the artifact records something the tree no longer has
  (``recorded - live``).  A phantom duration row inflates the shard-balancing
  estimate for a test that does not exist; a ranked-queue row names a deleted
  file.  This is a real defect and it must block.

* **ADDITION** -- the tree has something the artifact does not yet record
  (``live - recorded``).  Someone added a test.  Nothing is wrong with the
  code; the index simply has not caught up.

Both reds look identical today, so the routine case (adding a test) trains
readers to treat the gate as noise -- which is exactly how the genuine case
(rot reaching the promotion PR, where a red CLOSES the PR and deletes its
branch) gets waved through.

WHAT THIS MODULE CLAIMS, AND WHAT IT DOES NOT
---------------------------------------------
It claims only DIRECTION.  Direction is not disposition, and conflating the two
would be a second version of the same mistake:

* For the three durations artifacts the two directions have OPPOSITE correct
  responses, and the cheap one is the rot one.  Rot is repairable for free from
  a checkout -- deleting a row for a test that no longer exists is a total
  function of the tree.  An addition is UNPAYABLE from a checkout: the artifact
  records measured timings, and inventing one is worse than the red (see
  ``derived_artifact_repair.MEASUREMENT_TAX_GUIDANCE``).  That inversion is the
  single most useful thing this module reports.

* For ``test-estate-ranked-queue.md`` both directions are mechanical: the
  generator is a total function of the tree, and ``./check`` already repairs it.

* For a RATCHET, FLOOR, SNAPSHOT or CAP, direction is computable but changes
  nothing: an addition there is precisely the change a human is supposed to
  ratify.  Those sentinels are registered in :data:`DECISION_BEARING` rather
  than given a classifier, because standing up a second extractor beside the
  guard's own is how two inventories drift apart -- the failure mode
  ``NEVER_AUTOREPAIR`` already warns about for the emitter inventory.

* For ``test_conversational_workday_protection.py`` the axis does not apply at
  all.  Its ``test_count`` entries are FLOORS: ``recorded > live`` is EROSION
  (always a defect) and ``recorded < live`` never fails.  There is no "addition"
  failure to distinguish.  Registered in :data:`NOT_DIRECTIONAL`.

This module NEVER flips a red to green.  It is advisory: the guard-data lane
runs it after a failure to annotate the failure, and keeps the pytest exit code.

WHY THE GATE DOES NOT AUTO-COMMIT THE REPAIR
--------------------------------------------
The obvious next step -- have CI regenerate the artifact and push it to the PR
branch -- is wrong here, for reasons specific to this repository rather than
general squeamishness:

1. GitHub does not re-trigger workflows for a push made with ``GITHUB_TOKEN``.
   The auto-committed head would therefore carry ZERO check-runs, and
   ``ci_resolution.proceed_on_no_checks`` resolves an ABSENCE of checks as
   passable while a present red blocks.  Auto-committing would convert a
   blocking red into a silently passable no-checks head: strictly worse than
   the failure it replaced.
2. ``guard-data.yml`` runs with ``permissions: contents: read`` on a SHARED
   self-hosted runner whose workspace ci.yml also uses.  A write would need an
   operator escalation and would race a concurrent push.
3. The gate would be mutating the very thing it is judging.  A diff that
   changes code and the derived evidence of that code's correctness in one
   commit is self-certifying and unreviewable -- the failure mode CLAUDE.md's
   review rule names explicitly.

Auto-repair therefore stays where it already is and where it is safe: on the
AUTHOR's side, in ``./check`` -> ``scripts/sentinel_freshness.py --fix``, which
stages the regenerated artifact into the author's own commit where a reviewer
sees it.  What the CI lane owes the reader instead is an accurate name for the
failure and a copy-pasteable repair, which is what this module produces.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = [
    "Classification",
    "DerivedArtifact",
    "DIRECTIONAL",
    "DECISION_BEARING",
    "NOT_DIRECTIONAL",
    "FRESH",
    "ROT",
    "ADDITION",
    "MIXED",
    "classify",
    "classify_sentinel",
    "format_report",
    "format_unclassified",
]

REPO_ROOT = Path(__file__).resolve().parents[1]

FRESH = "fresh"
ROT = "rot"
ADDITION = "addition"
MIXED = "mixed"
ERROR = "error"

# How a given direction is discharged, quoted verbatim into the report so a
# reader never has to infer whether the fix is a command or a decision.
MECHANICAL = "mechanical"
MEASUREMENT = "measurement"


# ---------------------------------------------------------------------------
# Module loading
#
# The backend helpers this needs (estate_inventory, identity_extraction,
# durations_measurability) are stdlib-only and have no intra-package imports,
# so they are loaded BY PATH under private names rather than by inserting
# backend/ on sys.path and importing ``testing.*``. Two reasons: this module is
# imported from scripts/test_*.py inside a pytest process that may already have
# a different ``testing`` package bound, and it must also work against a
# synthetic fixture tree that contains only the files under test.
# ---------------------------------------------------------------------------


def _load(repo: Path, relative: str, alias: str):
    """Import a stdlib-only backend helper from *repo* under a private name.

    Two details are load-bearing and both were found the hard way:

    * The module MUST be registered in ``sys.modules`` before ``exec_module``.
      ``identity_extraction`` declares ``@dataclass(frozen=True)`` classes, and
      ``dataclasses._is_type`` resolves annotations through
      ``sys.modules[cls.__module__].__dict__`` -- an unregistered module makes
      that ``None.__dict__`` and the import dies with an ``AttributeError``
      that says nothing about the real cause.
    * The alias is qualified by the resolved path, so classifying a synthetic
      fixture tree in a test cannot hand back the real repository's module (or
      the other way round) out of the import cache.
    """
    path = (repo / relative).resolve()
    qualified = f"{alias}_{abs(hash(str(path))):x}"
    cached = sys.modules.get(qualified)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(qualified, path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable file
        raise FileNotFoundError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(qualified, None)
        raise
    return module


def _estate(repo: Path):
    return _load(repo, "backend/testing/estate_inventory.py", "_bpguard_estate")


def _identity(repo: Path):
    return _load(repo, "backend/testing/identity_extraction.py", "_bpguard_identity")


def _measurability(repo: Path):
    return _load(
        repo, "backend/testing/durations_measurability.py", "_bpguard_measurability"
    )


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# backend/.test_durations
# ---------------------------------------------------------------------------


def backend_duration_files(repo: Path) -> set[str]:
    """File keys recorded in ``backend/.test_durations``.

    Keys are ``testing/test_x.py::node``; the file half is what "does this test
    file still exist" is asked about.
    """
    data = _read_json(repo / "backend" / ".test_durations")
    return {key.split("::", 1)[0] for key in data if "::" in key}


def backend_test_files(repo: Path) -> set[str]:
    """Every backend test module ON DISK, measurable or not.

    This is the live set ROT is measured against, and it is deliberately NOT
    the measurable set below. A module that exists but currently cannot be
    timed -- it declares itself an intentional-zero-tests helper, or carries a
    module-level marker ``addopts`` deselects -- still exists, so its recorded
    rows are historical measurement, not phantom rows for a deleted file.
    ``backend/testing/test_invariants.py`` is exactly that shape today, and
    measuring rot against the measurable set would report it as stale and send
    a reader to delete real data.
    """
    backend = repo / "backend"
    return {
        path.relative_to(backend).as_posix()
        for path in _estate(repo).iter_backend_test_files(repo)
    }


def backend_measurable_files(repo: Path) -> set[str]:
    """Backend test modules a timed run can actually produce an entry for.

    Mirrors ``test_durations_coverage_fraction_within_threshold``: a module that
    executes no test (an intentional-zero-tests helper, or one deselected by the
    suite's own ``addopts``) can never appear in the artifact, so counting it as
    a missing ADDITION would manufacture a failure nothing can discharge.
    """
    estate = _estate(repo)
    measurability = _measurability(repo)
    backend = repo / "backend"
    deselected = measurability.deselected_markers(backend / "pyproject.toml")
    live: set[str] = set()
    for path in estate.iter_backend_test_files(repo):
        if measurability.unmeasurable_reason(path, deselected) is None:
            live.add(path.relative_to(backend).as_posix())
    return live


def backend_stale_entries(repo: Path) -> set[str]:
    """Every duration ROW whose test no longer exists, at ENTRY granularity.

    Two shapes, deliberately reported as one set: the row's file is gone, or the
    file survives and the node id inside it is gone. Both are phantom seconds
    that pytest-split sums into a shard, both are repaired by deleting the row,
    and a reader fixing them wants the exact keys -- not a file name for one
    shape and a node id for the other.

    ``prune_stale_durations`` deletes exactly this set, so the report and the
    repair cannot disagree about what is stale.
    """
    data = _read_json(repo / "backend" / ".test_durations")
    on_disk = backend_test_files(repo)
    stale = set(backend_orphaned_nodes(repo))
    for key in data:
        file_key, separator, _ = key.partition("::")
        if separator and file_key not in on_disk:
            stale.add(key)
    return stale


def backend_orphaned_nodes(repo: Path) -> set[str]:
    """Recorded entries whose FILE still exists but whose node id does not.

    This is the half ``test_durations_no_stale_entries`` already guards. It is
    recomputed here rather than parsed out of the pytest failure so the report
    stays precise even when the guard warn-passed the drift as inherited.
    """
    identity = _identity(repo)
    data = _read_json(repo / "backend" / ".test_durations")
    backend = repo / "backend"
    on_disk = {
        path.relative_to(backend).as_posix(): path
        for path in _estate(repo).iter_backend_test_files(repo)
    }
    ids_by_file: dict[str, set[str]] = {}
    orphaned: set[str] = set()
    for key in data:
        file_key, separator, raw_node = key.partition("::")
        if not separator or file_key not in on_disk:
            continue
        if file_key not in ids_by_file:
            ids_by_file[file_key] = identity.extract_python_test_ids_from_file(
                on_disk[file_key]
            )
        if raw_node.split("[", 1)[0] not in ids_by_file[file_key]:
            orphaned.add(key)
    return orphaned


# ---------------------------------------------------------------------------
# admin-ui/.test_durations.json, agent/.test_durations.json
# ---------------------------------------------------------------------------


def vitest_recorded(repo: Path, suite: str) -> set[str]:
    data = _read_json(repo / suite / ".test_durations.json")
    return {
        entry.get("file")
        for entry in data.get("files", [])
        if isinstance(entry, dict) and entry.get("file")
    }


def vitest_live(repo: Path, suite: str) -> set[str]:
    return _estate(repo).vitest_test_file_inventory(repo, suite)


# ---------------------------------------------------------------------------
# test-estate-ranked-queue.md
# ---------------------------------------------------------------------------

# Same row shape the guard itself matches. Kept here as a literal on purpose:
# the guard's regex is the contract, and a classifier that imported it would
# report "fresh" for a queue whose rows the guard can no longer parse.
#
# `| P2 | backend | `path` | ...` -- no leading rank ordinal. The ordinal was
# removed because it renumbered every row below an insertion, which is what made
# this artifact conflict on 40% of merges; see the generator's module docstring.
_RANKED_ROW = re.compile(r"^\| \S+ \| \S+ \| `([^`]+)` \|", re.MULTILINE)


def ranked_queue_recorded(repo: Path) -> set[str]:
    text = (repo / "test-estate-ranked-queue.md").read_text(encoding="utf-8")
    return set(_RANKED_ROW.findall(text))


# The queue's row set is every backend test module plus every Vitest file in
# these three suites, named repo-relative -- generate_test_estate_ranked_queue's
# collect_test_files() over exactly this list.
RANKED_QUEUE_SUITES = ("admin-ui", "admin-ui-viewport", "agent")


def ranked_queue_live(repo: Path) -> set[str]:
    """Paths the ranked queue should contain, derived from *repo* alone.

    Deliberately NOT by importing generate_test_estate_ranked_queue: that module
    inserts its own repository root on ``sys.path`` and imports
    ``backend.testing.*`` by package name, so importing it a second time against
    a different tree hands back whichever ``backend`` package sys.modules
    already holds. In-process that silently computes one repo's answer for
    another -- which would make every fixture-tree test of this classifier
    vacuous. The path accessors below are the same ones the generator uses, and
    ``test_ranked_queue_live_matches_the_generator`` pins the two together
    against the real tree.
    """
    estate = _estate(repo)
    live = {
        path.relative_to(repo).as_posix()
        for path in estate.iter_backend_test_files(repo)
    }
    for suite in RANKED_QUEUE_SUITES:
        live |= {
            path.relative_to(repo).as_posix()
            for path in estate.iter_vitest_test_files(repo, suite)
        }
    return live


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedArtifact:
    """One artifact whose recorded and live sets are both readable from a tree."""

    sentinel: str
    artifact: str
    unit: str
    recorded: Callable[[Path], set[str]]
    live: Callable[[Path], set[str]]
    rot_disposition: str
    rot_repair: str
    addition_disposition: str
    addition_repair: str
    rot: Callable[[Path], set[str]] | None = None
    """Rot computed directly, when ``recorded - live`` is the wrong question.

    It is the wrong question whenever the two directions are asked at different
    granularities or against different live sets. ``backend/.test_durations`` is
    both: additions are asked per FILE and only of files a timed run could
    measure, while rot is asked per ENTRY and against every file ON DISK -- a
    module that exists but cannot be timed today (an intentional-zero-tests
    helper) keeps its historical rows and is not rot. Defaults to
    ``recorded - live`` when the two coincide.
    """


def _vitest_entry(suite: str) -> DerivedArtifact:
    return DerivedArtifact(
        sentinel="backend/testing/test_vitest_durations_freshness.py",
        artifact=f"{suite}/.test_durations.json",
        unit="vitest file entry",
        recorded=(lambda repo, suite=suite: vitest_recorded(repo, suite)),
        live=(lambda repo, suite=suite: vitest_live(repo, suite)),
        rot_disposition=MECHANICAL,
        rot_repair="python scripts/prune_stale_durations.py --write",
        addition_disposition=MEASUREMENT,
        addition_repair=(
            f"node scripts/vitest-test-telemetry.mjs --suite {suite}  # a timed "
            "vitest run. The guard refuses fabricated rows "
            "(_assert_all_entries_measured rejects measured:false and "
            "zero-filled durations), so there is no legal placeholder."
        ),
    )


DIRECTIONAL: dict[str, tuple[DerivedArtifact, ...]] = {
    "backend/testing/test_test_durations_freshness.py": (
        DerivedArtifact(
            sentinel="backend/testing/test_test_durations_freshness.py",
            artifact="backend/.test_durations",
            unit="duration entry",
            recorded=backend_duration_files,
            live=backend_measurable_files,
            rot=backend_stale_entries,
            rot_disposition=MECHANICAL,
            rot_repair="python scripts/prune_stale_durations.py --write",
            addition_disposition=MEASUREMENT,
            addition_repair=(
                "bash scripts/store-backend-durations.sh  # a full timed run: "
                "Docker + Postgres, 15-20 min. There is no offline shortcut, and "
                "hand-writing a duration is worse than the red -- pytest-split "
                "already estimates an unknown test from the average, while a "
                "fabricated row marks the file covered so no later run ever "
                "measures it."
            ),
        ),
    ),
    "backend/testing/test_vitest_durations_freshness.py": (
        _vitest_entry("admin-ui"),
        _vitest_entry("agent"),
    ),
    "backend/testing/test_test_estate_ranked_queue.py": (
        DerivedArtifact(
            sentinel="backend/testing/test_test_estate_ranked_queue.py",
            artifact="test-estate-ranked-queue.md",
            unit="ranked row",
            recorded=ranked_queue_recorded,
            live=ranked_queue_live,
            rot_disposition=MECHANICAL,
            rot_repair="python scripts/generate_test_estate_ranked_queue.py --write",
            addition_disposition=MECHANICAL,
            addition_repair=(
                "python scripts/generate_test_estate_ranked_queue.py --write  # "
                "./check already does this for you via "
                "sentinel_freshness.py --fix"
            ),
        ),
    ),
}


DECISION_BEARING: dict[str, str] = {
    "backend/testing/test_event_type_invariants.py": (
        "event_type_snapshot.json is a RATCHET of pinned literals. Direction is "
        "computable, but it does not change the disposition: an addition is a "
        "new event_type nobody has classified yet, which is precisely the change "
        "the ratchet exists to make a human ratify, and a removal is a literal "
        "that must be retired deliberately. Both block. No classifier is "
        "registered because the second extractor needed to compute the direction "
        "would be a second inventory beside the guard's own, and those drift."
    ),
    "backend/testing/test_websocket_emitter_inventory.py": (
        "The fixture is a reviewed SNAPSHOT of the websocket surface, held "
        "deliberately in parallel with the hand-written "
        "WEBSOCKET_EMITTER_INVENTORY dict. An addition is a NEW EMITTER, which "
        "is the reviewable event, not an index lagging behind. Same disposition "
        "in both directions, so classifying buys the reader nothing the guard's "
        "own message does not already say."
    ),
    "backend/testing/test_operator_workbench_contracts.py": (
        "Its emitter twin lives in runtime code and has no generator at all -- "
        "the repair is a hand edit by design. Nothing here is the index has not "
        "caught up; both directions are a contract change."
    ),
    "backend/testing/test_conversational_workday_protection.py": (
        "The per-file test_count entries in conversational_workday_manifest.json "
        "are EXACT pins, not floors -- `_find_test_count_mismatches` reports "
        "`declared_exact=` and `test_new_floor_slack_relative_to_integration_"
        "branch_fails` exists precisely to prove that declaring LESS than the "
        "tree has still fails. Direction is computable, but it does not change "
        "the disposition: recorded > live is EROSION (coverage was deleted from "
        "a protected surface, always a defect), and recorded < live is coverage "
        "that was ADDED and not yet ratified. Both block, and both are repaired "
        "by a human editing the manifest deliberately -- upward to ratify the "
        "new tests, never downward to match a deletion."
    ),
}


# Sentinels whose artifact has no rot/addition axis at all -- where "the tree
# grew a thing the artifact does not record" is not a state the artifact can
# be in. Empty is a legitimate state for this registry, not a sign it is dead:
# a sentinel belongs here only when the direction is genuinely UNCOMPUTABLE,
# not merely when both directions happen to block (that is DECISION_BEARING).
NOT_DIRECTIONAL: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    sentinel: str
    artifact: str
    verdict: str
    rot: tuple[str, ...] = ()
    additions: tuple[str, ...] = ()
    unit: str = "entry"
    rot_disposition: str = MECHANICAL
    rot_repair: str = ""
    addition_disposition: str = MECHANICAL
    addition_repair: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "sentinel": self.sentinel,
            "artifact": self.artifact,
            "verdict": self.verdict,
            "rot": list(self.rot),
            "additions": list(self.additions),
            "unit": self.unit,
            "rot_disposition": self.rot_disposition,
            "rot_repair": self.rot_repair,
            "addition_disposition": self.addition_disposition,
            "addition_repair": self.addition_repair,
            "error": self.error,
        }


def _verdict(rot: set[str], additions: set[str]) -> str:
    if rot and additions:
        return MIXED
    if rot:
        return ROT
    if additions:
        return ADDITION
    return FRESH


def classify_artifact(repo: Path, entry: DerivedArtifact) -> Classification:
    """Compare *entry*'s recorded set against the live tree.

    Fails OPEN into an ``error`` classification rather than raising: this is an
    advisory annotator running after a failure that already blocks, and an
    exception here would replace a precise guard message with a traceback.
    """
    try:
        recorded = set(entry.recorded(repo))
        live = set(entry.live(repo))
        rot = (recorded - live) if entry.rot is None else set(entry.rot(repo))
        additions = live - recorded
    except Exception as exc:  # noqa: BLE001 - advisory lane, never raise
        return Classification(
            sentinel=entry.sentinel,
            artifact=entry.artifact,
            verdict=ERROR,
            unit=entry.unit,
            error=f"{type(exc).__name__}: {exc}",
        )
    return Classification(
        sentinel=entry.sentinel,
        artifact=entry.artifact,
        verdict=_verdict(rot, additions),
        rot=tuple(sorted(rot)),
        additions=tuple(sorted(additions)),
        unit=entry.unit,
        rot_disposition=entry.rot_disposition,
        rot_repair=entry.rot_repair,
        addition_disposition=entry.addition_disposition,
        addition_repair=entry.addition_repair,
    )


def classify_sentinel(repo: Path, sentinel: str) -> list[Classification]:
    return [classify_artifact(repo, entry) for entry in DIRECTIONAL.get(sentinel, ())]


def classify(repo: Path, sentinels: list[str] | None = None) -> list[Classification]:
    """Classify every directional artifact, or only those under *sentinels*."""
    selected = sorted(DIRECTIONAL) if sentinels is None else sorted(set(sentinels))
    results: list[Classification] = []
    for sentinel in selected:
        results.extend(classify_sentinel(repo, sentinel))
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_PREFIX = "[derived-artifact]"
_SAMPLE = 10


def _sample(names: tuple[str, ...]) -> str:
    shown = list(names[:_SAMPLE])
    more = len(names) - len(shown)
    text = ", ".join(shown)
    return f"{text} (+{more} more)" if more else text


def format_report(results: list[Classification]) -> str:
    lines: list[str] = []
    for result in results:
        if result.verdict == ERROR:
            lines.append(
                f"{_PREFIX} {result.artifact}: could not classify -- {result.error}"
            )
            continue
        if result.verdict == FRESH:
            lines.append(f"{_PREFIX} {result.artifact}: fresh in both directions")
            continue
        if result.rot:
            lines.append(
                f"{_PREFIX} ROT -- {result.artifact} records {len(result.rot)} "
                f"{result.unit}(s) the tree no longer has. This is a real defect: "
                "it must be repaired, not waited out."
            )
            lines.append(f"{_PREFIX}   stale: {_sample(result.rot)}")
            lines.append(
                f"{_PREFIX}   repair ({result.rot_disposition}): {result.rot_repair}"
            )
        if result.additions:
            lines.append(
                f"{_PREFIX} ADDITION, NOT ROT -- the tree has "
                f"{len(result.additions)} {result.unit}(s) {result.artifact} does "
                "not record yet. Nothing is broken; the index has not caught up."
            )
            lines.append(f"{_PREFIX}   missing: {_sample(result.additions)}")
            lines.append(
                f"{_PREFIX}   repair ({result.addition_disposition}): "
                f"{result.addition_repair}"
            )
    return "\n".join(lines)


def format_unclassified() -> str:
    """The honest half of the report: what this tool refuses to classify."""
    lines: list[str] = []
    if DECISION_BEARING:
        lines.append(
            f"{_PREFIX} not classified (direction is computable but does not "
            "change the disposition -- an addition here IS the reviewable "
            "event):"
        )
        for sentinel, why in sorted(DECISION_BEARING.items()):
            lines.append(f"{_PREFIX}   {sentinel}: {why}")
    if NOT_DIRECTIONAL:
        lines.append(
            f"{_PREFIX} not classified (the rot/addition axis does not apply):"
        )
        for sentinel, why in sorted(NOT_DIRECTIONAL.items()):
            lines.append(f"{_PREFIX}   {sentinel}: {why}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify derived-artifact drift.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "sentinels",
        nargs="*",
        help="Sentinel paths to classify; default is every directional artifact.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    parser.add_argument(
        "--explain-unclassified",
        action="store_true",
        help=(
            "Also print why the remaining DERIVED-ARTIFACT sentinels (the "
            "static_release_guard.SENTINEL_REPAIR_COMMANDS population) are not "
            "classified. The rest of the guard-data roster -- shard balance, "
            "the impacted-test resolver, the migration chain, the promotion "
            "metric manifest -- are not index files and have no rot/addition "
            "axis to report."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero when anything is not fresh. OFF by default: in CI "
            "this lane is ADVISORY and must never be what turns the gate red or "
            "green -- the guards themselves own that."
        ),
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    results = classify(repo, args.sentinels or None)

    if args.json:
        print(json.dumps([result.as_dict() for result in results], indent=2))
    else:
        report = format_report(results)
        if report:
            print(report)
        if args.explain_unclassified:
            print(format_unclassified())

    if args.check and any(result.verdict != FRESH for result in results):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
