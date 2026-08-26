"""Live-tree module-size guardrail, on a lane an ``automain`` PR actually runs.

THE DEFECT THIS CLOSES
----------------------
``backend/testing/check_size_limits.py`` and the coverage meta-tests in
``test_invariants.py`` already encode the size guardrail: a governed source file
over its ``module_size_limits.json`` cap is a violation, and a governed source
file over ``_COVERAGE_THRESHOLD`` lines with NO cap entry at all is a violation.
What was missing was a lane that *runs a live-tree size scan on an automain PR*:

* ``.github/workflows/ci.yml`` — which carries the backend matrix that runs
  ``test_invariants.py`` — triggers on ``pull_request`` into ``main`` only.
  An ``automain`` PR gets zero ci.yml checks by design.
* ``.github/workflows/guard-data.yml`` is the only workflow an ``automain`` PR
  does get. Its "Derived artifacts" job replaces the standalone roster wholesale
  via ``BLUEPRINT_STATIC_GUARD_SELECTORS`` (no size scan in that list), and its
  "Impacted backend tests" job runs only what
  ``backend/testing/impacted_tests.py`` selects for the diff. Before this file
  existed, NO entry in ``drift_sentinel_surfaces.json`` mapped a governed source
  surface (``backend/app``, ``backend/testing``, ``agent/src``,
  ``admin-ui/src``, ``scripts``) to any live-tree size scan, so a diff that grew
  a file past its cap selected nothing that would notice.

Result: a size breach first turned red on the ``automain`` -> ``main``
promotion PR, which is the worst possible place to find it — a red promotion PR
usually closes and deletes its branch, forcing a re-cut.

PROOF the gap was live, not theoretical: PR #17966 took
``backend/testing/test_impacted_tests.py`` from 1431 to 1522 lines — past the
1500-line uncapped threshold, with no cap entry — and merged into ``automain``
green.

WHY A SEPARATE FILE rather than leaning on ``test_invariants.py``
----------------------------------------------------------------
``test_invariants.py`` does hold the equivalent scans, but it is ~15.7k lines
and pulls in the app/pytest machinery. Registering *it* against every governed
source glob would drag that whole module into every impacted run AND into
``scripts/sentinel_freshness.py``, which runs matched sentinels in BOTH the task
tree and the base tree on every ``./check``. This module is the small, cheap,
dependency-light scan that can afford to be mandatory.

ANTI-VACUITY
------------
Three of this repo's guards have already been caught reporting clean because
they could not look (#17872, #17920). So this module NEVER degrades to a silent
pass: the repo root is resolved through the sentinel-verified
``repo_scripts_path`` resolver (not a bare ``isdir``), an unresolvable root is a
hard failure under CI, and ``test_scan_actually_reached_the_live_tree`` pins
that each governed section resolved to a directory holding a plausible number of
real source files. See ``test_live_tree_size_gate_fails_on_a_planted_breach``
for the in-band mutation proof that the scan can still say no.

COST, AND WHY THE MARKER
------------------------
This is a corpus scan: every governed source file is read once for its line
count and once to discover a ``*_SIZE_GOVERNANCE_CAP`` declaration. The cost is
file I/O, so it depends ENTIRELY on the filesystem underneath -- and the two
filesystems this runs on are ~35x apart. Both numbers are recorded here,
measured 3x each on this tree, in the standalone lane's own container image:

    repo on the container's own filesystem    2.53s / 2.68s / 2.68s whole file
      -- what a Linux CI runner does          slowest single test  1.47s

    repo on a Docker-on-Windows bind mount   103.1s / 88.9s / 78.3s whole file
      -- what ``./check test`` does here      slowest single test 39.71s

Same code, same container: ~7-9ms per file open across the bind mount against
~5us on a local filesystem, over ~1400 files read up to three times. The scan
is cheap in CI and expensive on a Windows developer box.

The standalone lane runs ``pytest --timeout=30``. In CI that is ~12x headroom
and no marker would be needed; across the bind mount the two big sections land
on the WRONG SIDE of it, and a slow filesystem then gets reported as a size
defect -- the harness-budget-inversion class that killed promotion PR #17674.
The marker below exists for that second case and only that case. 120s is the
house budget for corpus-scanning guards (``test_string_patch_targets_resolve``
and ``test_mock_patch_targets_resolve`` in test_invariants.py both carry it
for the same reason) and leaves ~3x headroom over the slowest bind-mount figure.

The parse half of the cost was already removed:
``size_limit_policy.governance_caps_from_source_text`` now screens for the
literal before paying for ``ast.parse``, which took a five-section scan from
21.25s to 1.86s on a native filesystem. What remains is file I/O, not parsing.
A follow-up could halve it again by reading each file ONCE for both the line
count and the governance declaration -- today ``scan_uncapped_section`` and
``scan_governed_missing_cap`` each re-read the same files.
"""

from __future__ import annotations

import os

import pytest

from testing import check_size_limits as csl
from testing.fixgate_attribution import format_offender_marker
from testing.repo_scripts_path import resolve_scripts_dir_from_env

# See "COST, AND WHY THE MARKER" above. Module-level because every test here is
# the same shape of corpus scan; the cheap sections simply never approach it.
pytestmark = pytest.mark.timeout(120)

# The five governed sections, in the order ``check_size_limits.SECTIONS``
# declares them. Derived, never re-listed: a section added to the checker
# without a seat here would otherwise be scanned by the CLI and silently
# skipped by this gate. ``test_every_governed_section_has_a_live_tree_test``
# pins the derivation.
GOVERNED_SECTIONS = tuple(csl.SECTIONS)

# Backend dir (``/app`` in the Docker test image, ``<repo>/backend`` locally).
_BACKEND_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# The repo root is resolved from the sentinel-verified scripts/ resolver rather
# than a fixed ``../..`` walk: in the Docker test image the backend tree is
# mounted at ``/app`` with no ``scripts/`` sibling, so ``../..`` resolves to
# ``/`` and every section root would come back missing -- a guard that reports
# clean because it never looked. ``resolve_scripts_dir_from_env`` requires a
# real marker file inside the candidate, so a directory that merely exists can
# never be mistaken for the repo.
_SCRIPTS_DIR, _SCRIPTS_DIR_UNAVAILABLE = resolve_scripts_dir_from_env(_BACKEND_DIR)
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR) if _SCRIPTS_DIR else None

# A section root is allowed to be absent only in an environment that genuinely
# does not carry that tree. Under CI every tree is present (the harness mounts
# the repo root at /repo), so an absent root there is a harness defect, not a
# reason to pass.
_SECTION_ROOT_ABSENT = (
    "{section}: source root did not resolve under {repo_root}. The size "
    "guardrail cannot see this section, so it CANNOT be reported clean."
)


def _require_repo_root() -> str:
    """Return the repo root, or fail -- HARD under CI, skip only ad hoc.

    Mirrors ``test_invariants._require_scripts_root``: a silently unresolvable
    repo root would blind every cap in ``module_size_limits.json``, which is
    exactly the failure mode this file exists to prevent, so CI never skips.
    """
    if _REPO_ROOT is None:
        message = (
            f"{_SCRIPTS_DIR_UNAVAILABLE}; the size guardrail resolves the repo "
            "root from it, so the CI harness must mount the repo at /repo"
        )
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return _REPO_ROOT


def _require_section_root(section: str) -> str:
    repo_root = _require_repo_root()
    root = csl.resolve_section_root(section, repo_root)
    if root is None:
        message = _SECTION_ROOT_ABSENT.format(section=section, repo_root=repo_root)
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return root


def _load_caps(section: str) -> dict:
    repo_root = _require_repo_root()
    limits_path = os.path.join(
        repo_root, "backend", "testing", "module_size_limits.json"
    )
    return csl.load_limits(limits_path).get(section) or {}


def live_tree_violations(section: str) -> list[str]:
    """Every size violation the live tree carries for ``section``.

    Three violation classes, matching ``check_size_limits.py``'s check mode:

      1. a file with a JSON cap that is now over its effective cap;
      2. a file that declares its own ``*_SIZE_GOVERNANCE_CAP`` with no JSON
         entry, over that declared cap;
      3. a file over ``_COVERAGE_THRESHOLD`` lines with no cap entry at all --
         the class that let ``test_impacted_tests.py`` reach 1522 lines.

    Returned as human-readable strings so the failure message names the file,
    its count, and the number it breached instead of a bare count mismatch.
    """
    repo_root = _require_repo_root()
    root = _require_section_root(section)
    caps = _load_caps(section)
    suffixes, is_excluded = csl._SECTION_SCAN[section]

    violations: list[str] = []
    records = csl.scan_section(caps, root, section, repo_root=repo_root)
    records.extend(
        csl.scan_governed_missing_cap(root, caps, section, repo_root=repo_root)
    )
    for record in records:
        if not record.get("breach"):
            continue
        violations.append(
            f"[over-cap] {section}/{record['rel']}: {record['count']} lines "
            f"(cap: {record.get('effective_cap')})"
        )

    for offender in csl.scan_uncapped_section(
        root,
        caps,
        suffixes,
        is_excluded,
        section=section,
        repo_root=repo_root,
    ):
        violations.append(
            f"[uncapped] {section}/{offender['rel']}: {offender['count']} lines "
            f"(threshold: {csl._COVERAGE_THRESHOLD}) - NO cap entry"
        )
    return sorted(violations)


def _assert_clean(section: str) -> None:
    violations = live_tree_violations(section)
    assert not violations, (
        f"{len(violations)} live-tree size violation(s) in the '{section}' "
        "section:\n  "
        + "\n  ".join(violations)
        + "\n\nRepair with: python backend/testing/check_size_limits.py --fix "
        f"--sections {section}\n"
        "Do NOT raise a cap to dodge a real decomposition -- see "
        "backend/testing/cap_bump_history.json.\n"
        + format_offender_marker(violations)
    )


def test_backend_live_tree_has_no_size_violation():
    _assert_clean("backend")


def test_testing_live_tree_has_no_size_violation():
    _assert_clean("testing")


def test_scripts_live_tree_has_no_size_violation():
    _assert_clean("scripts")


def test_agent_live_tree_has_no_size_violation():
    _assert_clean("agent")


def test_frontend_live_tree_has_no_size_violation():
    if os.environ.get("SKIP_FRONTEND_SIZE_GUARDRAILS") == "1":
        pytest.skip("SKIP_FRONTEND_SIZE_GUARDRAILS=1")
    _assert_clean("frontend")


def test_every_governed_section_has_a_live_tree_test():
    """A section the checker governs must have a seat above.

    Without this, adding a sixth section to ``check_size_limits.SECTIONS``
    would leave it scanned by the CLI and invisible to the only lane an
    ``automain`` PR runs -- reintroducing the exact gap this file closes.
    """
    covered = {
        name[len("test_") : -len("_live_tree_has_no_size_violation")]
        for name in globals()
        if name.startswith("test_") and name.endswith("_live_tree_has_no_size_violation")
    }
    missing = [
        f"section-without-test:{name}"
        for name in sorted(set(GOVERNED_SECTIONS) - covered)
    ]
    unknown = [
        f"test-without-section:{name}"
        for name in sorted(covered - set(GOVERNED_SECTIONS))
    ]
    assert covered == set(GOVERNED_SECTIONS), (
        f"governed sections without a live-tree test: {missing}; live-tree "
        f"tests for unknown sections: {unknown}\n"
        + format_offender_marker(missing + unknown)
    )


def test_scan_actually_reached_the_live_tree():
    """Anti-vacuity: prove the scan looked at real files, not an empty tree.

    A resolver that returns a directory which merely exists (an empty mount, a
    stale path) would make every assertion above pass while checking nothing.
    Each governed section is pinned to a floor well under its real inventory
    but far above zero, so an empty or wrong root fails loudly.
    """
    repo_root = _require_repo_root()
    floors = {
        "backend": 100,
        "agent": 20,
        "frontend": 50,
        "testing": 100,
        "scripts": 10,
    }
    assert set(floors) == set(GOVERNED_SECTIONS)
    seen: dict[str, int] = {}
    for section in GOVERNED_SECTIONS:
        if section == "frontend" and (
            os.environ.get("SKIP_FRONTEND_SIZE_GUARDRAILS") == "1"
        ):
            continue
        root = csl.resolve_section_root(section, repo_root)
        if root is None:
            message = _SECTION_ROOT_ABSENT.format(section=section, repo_root=repo_root)
            if os.environ.get("CI"):
                pytest.fail(message)
            pytest.skip(message)
        suffixes, is_excluded = csl._SECTION_SCAN[section]
        count = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(suffixes):
                    continue
                rel = os.path.relpath(
                    os.path.join(dirpath, name), root
                ).replace(os.sep, "/")
                if is_excluded(rel):
                    continue
                count += 1
        seen[section] = count
    short = [
        f"{section}: reached {count} source files under "
        f"{csl.resolve_section_root(section, repo_root)}, floor {floors[section]}"
        for section, count in sorted(seen.items())
        if count < floors[section]
    ]
    assert not short, (
        "these governed sections reached far fewer source files than this "
        "repo carries, so the guard is looking at the wrong tree and would "
        "report clean without checking anything:\n  "
        + "\n  ".join(short)
        + "\n"
        + format_offender_marker(short)
    )


def test_live_tree_size_gate_fails_on_a_planted_breach(tmp_path):
    """Mutation proof, in-band: a planted over-threshold file must be caught.

    A guard that cannot be shown to fail is not a guard. This plants the exact
    shape of the live breach that merged green (an uncapped source file past
    ``_COVERAGE_THRESHOLD``) into a hermetic tree and asserts the same scanner
    call used by the live-tree tests above reports it -- so a future refactor
    that neuters the scan cannot leave these tests passing.
    """
    root = tmp_path / "backend" / "testing"
    root.mkdir(parents=True)
    (root / "test_planted_offender.py").write_text(
        "\n".join(f"# line {i}" for i in range(csl._COVERAGE_THRESHOLD + 1)) + "\n",
        encoding="utf-8",
    )
    suffixes, is_excluded = csl._SECTION_SCAN["testing"]
    offenders = csl.scan_uncapped_section(str(root), {}, suffixes, is_excluded)
    assert [offender["rel"] for offender in offenders] == ["test_planted_offender.py"]
    assert offenders[0]["count"] > csl._COVERAGE_THRESHOLD

    # ...and a cap entry (the legitimate repair) clears it.
    cleared = csl.scan_uncapped_section(
        str(root),
        {"test_planted_offender.py": csl._COVERAGE_THRESHOLD + 200},
        suffixes,
        is_excluded,
    )
    assert cleared == []
