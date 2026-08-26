"""
Standalone guard: no test may wrap a subprocess in a timeout at or above the
inner-operation ceiling without declaring an explicit pytest-timeout marker.

THE DEFECT CLASS THIS EXISTS TO PREVENT
---------------------------------------
When an inner operation's own timeout is >= the outer harness budget there is
zero headroom, so the harness kills the operation before the operation's own
timeout can ever report anything. A perfectly healthy call dies as
``Failed: Timeout (>30.0s) from pytest-timeout``, a random shard of a healthy
candidate goes red, and it presents as an unreproducible flake -- the inner
timeout is dead code that can never fire.

On a promotion PR a red run DELETES the PR and its release branch, so this
class is promotion-killing. It has already fired at least three times:

  * promotion PR #17674 -- the scripts lane booted ``local_runner.py --check``
    for real under a 120s budget while ``--check`` ran a 1200s venv+pip
    provision (root-fixed in #17681, which took ``--check`` to 0.44s);
  * ``test_cap_bump_history_ledger_matches_git`` -- ``subprocess.run(timeout=30)``
    under a 30.0s pytest-timeout, identical numbers, so any work before or
    after the subprocess guarantees the outer kill wins;
  * the scripts lane generally -- 448 tests in 52.0s under host load against a
    25s budget.

#17870 fixed the sibling case (slow pure-Python corpus scans) by raising the
marker. This guard covers the subprocess case, which that PR did not touch.

THE RULE
--------
Either the inner timeout is comfortably below the lane budget, or the test
carries an explicit ``@pytest.mark.timeout(M)`` with ``M`` greater than the
inner timeout. Equal numbers are a bug, not a tight fit.

A *helper* (a non-test function such as ``_run_check``) may never carry an
inner timeout at or above the lane budget: a helper cannot know which budget
its callers run under, and a marker on the helper does nothing.

Runs under --noconftest (no DB, no conftest fixtures).
"""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# TWO NUMBERS, TWO JOBS. These used to be a single constant at 30, which made
# one literal do two incompatible things.
#
# 1. LANE_HANG_DETECTOR_SECONDS -- the pytest-timeout budget every backend CI
#    lane passes on the command line. Its ONLY job is to catch a genuine hang
#    (deadlock, lost socket, unbounded retry) before GitHub Actions' 360-min
#    default stalls the runner for six hours. It is deliberately generous.
#    At 30s it was ALSO acting as a per-test performance budget, and in that
#    role it kept killing healthy work under host I/O contention: three
#    distinct victims across two local standalone runs (never the same test
#    twice, all whole-tree read+parse scans over 500-1000 files), plus the
#    shard-4 red that blocked release v45 on a candidate that had gone green
#    at the identical sha 40 minutes earlier. A hang detector that fires on
#    contention is a flake generator, and on a promotion PR a flake deletes
#    the release branch.
#
# 2. MAX_INNER_OPERATION_SECONDS -- the ceiling at or above which a single
#    inner blocking call (subprocess / Event.wait / join / acquire) must
#    declare itself with an explicit ``@pytest.mark.timeout(M)`` where M
#    exceeds the inner timeout. This is the budget-inversion guard from the
#    docstring above and it KEEPS ITS TEETH AT 30. Raising it alongside the
#    hang detector is precisely the mistake this split exists to prevent: it
#    has caught a promotion-killing class at least three times, and a 30s
#    inner call is a code smell whether or not the harness now survives it.
#
# Both are pinned as literals ON PURPOSE. Reading them out of the config files
# would make this guard self-certifying: bumping ci.yml would silently relax
# every assertion below. Instead the literals are the authority and
# ``test_declared_lane_budget_matches_the_pin`` proves the config still agrees
# with them, so a deliberate bump has to touch these lines and be reviewed.
LANE_HANG_DETECTOR_SECONDS = 300
MAX_INNER_OPERATION_SECONDS = 30

_SUBPROCESS_CALLS = frozenset(
    {"run", "check_output", "check_call", "call", "Popen"}
)

# The class is not specific to subprocess. PR #17882 introduced a fresh
# instance within hours of this sweep in a different shape -- an
# ``Event.wait(timeout=30.0)`` under the same 30s lane -- which is the same
# bug exactly: the wait can only ever be killed by the harness, never report
# itself. These three take their timeout in SECONDS unambiguously, which is
# why the family stops here: Playwright-style APIs spell it ``wait_for_*`` and
# count MILLISECONDS, so folding them in would compare incommensurable units.
_BLOCKING_WAIT_CALLS = frozenset({"wait", "join", "acquire"})

# A scan that silently matches nothing is worse than no scan: it reports green
# forever. These floors are far below the real counts (628 files / 15 calls
# under backend/testing, 39 files / 12 calls under scripts as of 2026-08-25)
# and exist only so a broken path or a changed layout fails loudly instead of
# passing vacuously.
#
# They are declared PER ROOT because the two lanes see different trees: the
# sharded backend lane runs inside the Docker image, which carries backend/ as
# /app and has no scripts/ directory at all, while the standalone lane runs
# from the checkout and sees both. A single combined floor would either be too
# low to be meaningful in the standalone lane or would false-fail in Docker --
# the first draft of this guard did exactly the latter.
_ROOT_FLOORS = {
    "backend/testing": {"files": 400, "calls": 10},
    "scripts": {"files": 25, "calls": 8},
}


def _repo_root() -> Path:
    """Resolve the checkout root from this file, never from the CWD.

    CWD-relative discovery is how a guard becomes vacuous: the standalone lane
    runs from ``backend/`` and the sharded lane from ``/app``, so a glob rooted
    at "." scans a different (or empty) tree in each. CI mounts the full
    checkout separately at ``/repo`` while the backend image is mounted at
    ``/app``, so prefer that validated mount when it is available.
    """
    here = Path(__file__).resolve()
    candidates = (Path("/repo"), *here.parents)
    for candidate in candidates:
        if (candidate / "backend" / "testing").is_dir():
            return candidate
    # Docker-only layout: the image carries backend/ as /app with no repo root.
    return here.parents[1]


def _repo_root_is_checkout() -> bool:
    """True when _repo_root() found a real checkout rather than falling back."""
    here = Path(__file__).resolve()
    return any((c / "backend" / "testing").is_dir() for c in here.parents)


REPO_ROOT = _repo_root()
BACKEND_TESTING = REPO_ROOT / "backend" / "testing"
if not BACKEND_TESTING.is_dir():
    BACKEND_TESTING = Path(__file__).resolve().parent
def _scripts_dir() -> Path:
    """Locate the scripts/ TEST corpus -- not merely a directory named scripts.

    ``.is_dir()`` cannot tell these three layouts apart, and picking the wrong
    one is how this guard went half-blind:

    * developer checkout -- ``<root>/scripts`` holds the corpus.
    * containerised lanes -- ``_repo_root()`` falls back to ``/app``, which is
      the bind-mounted ``backend/``. ``/app/scripts`` therefore resolves to
      ``backend/scripts``: seven analysis helpers and ZERO ``test_*.py``. The
      directory exists, so an ``.is_dir()`` skip never fires and the corpus
      floor asserts ``0 >= 25``. Same shape as ``.git`` being a FILE in a
      linked worktree (#17872) -- the existence check succeeds while the thing
      the guard actually needs is absent.
    * both Docker lanes mount the real checkout read-only at ``/repo``
      (scripts/test-backend.sh, "Mount the repo root read-only"), so the corpus
      IS reachable there. Skipping would have silenced a guard that can run.

    Select by the corpus itself, then. Fall back to the canonical path so the
    failure messages still name where we looked.
    """
    for candidate in (REPO_ROOT / "scripts", Path("/repo") / "scripts"):
        if any(candidate.glob("test_*.py")):
            return candidate
    return REPO_ROOT / "scripts"


SCRIPTS_DIR = _scripts_dir()


def _iter_test_files() -> list[tuple[str, Path]]:
    """Yield (root_label, path) so coverage can be asserted per root."""
    found: list[tuple[str, Path]] = [
        ("backend/testing", p) for p in sorted(BACKEND_TESTING.glob("test_*.py"))
    ]
    if SCRIPTS_DIR.is_dir():
        found.extend(
            ("scripts", p) for p in sorted(SCRIPTS_DIR.glob("test_*.py"))
        )
    return found


def _timeout_marker_seconds(node: ast.AST) -> float | None:
    """Return the seconds in a ``@pytest.mark.timeout(N)`` decorator, if any."""
    for decorator in getattr(node, "decorator_list", []):
        text = ast.unparse(decorator)
        if "mark.timeout" not in text:
            continue
        match = re.search(r"mark\.timeout\(\s*([0-9]+(?:\.[0-9]+)?)", text)
        if match:
            return float(match.group(1))
    return None


def _module_marker_seconds(tree: ast.Module) -> float | None:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "pytestmark" for t in node.targets):
            continue
        text = ast.unparse(node.value)
        match = re.search(r"mark\.timeout\(\s*([0-9]+(?:\.[0-9]+)?)", text)
        if match:
            return float(match.group(1))
    return None


class _Finding:
    def __init__(self, path: Path, lineno: int, seconds: float, owner: str,
                 owner_is_test: bool, budget: float | None):
        self.path = path
        self.lineno = lineno
        self.seconds = seconds
        self.owner = owner
        self.owner_is_test = owner_is_test
        self.budget = budget

    def __str__(self) -> str:
        rel = self.path.relative_to(REPO_ROOT) if REPO_ROOT in self.path.parents \
            else self.path.name
        if not self.owner_is_test:
            return (
                f"{rel}:{self.lineno}: helper {self.owner}() wraps a subprocess "
                f"in timeout={self.seconds:g}s, at or above the "
                f"{MAX_INNER_OPERATION_SECONDS}s inner-operation ceiling. A helper "
                f"cannot carry a pytest-timeout marker that "
                f"applies to its callers -- lower the inner timeout instead."
            )
        if self.budget is None:
            return (
                f"{rel}:{self.lineno}: {self.owner}() wraps a subprocess in "
                f"timeout={self.seconds:g}s, at or above the "
                f"{MAX_INNER_OPERATION_SECONDS}s inner-operation ceiling, with no "
                f"@pytest.mark.timeout declaring the budget it needs."
            )
        return (
            f"{rel}:{self.lineno}: {self.owner}() wraps a subprocess in "
            f"timeout={self.seconds:g}s but its @pytest.mark.timeout({self.budget:g}) "
            f"leaves no headroom. The marker must exceed the inner timeout."
        )


def _collect() -> tuple[list[_Finding], dict[str, dict[str, int]]]:
    findings: list[_Finding] = []
    coverage: dict[str, dict[str, int]] = {}
    for root_label, path in _iter_test_files():
        counts = coverage.setdefault(root_label, {"files": 0, "calls": 0})
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        counts["files"] += 1
        module_budget = _module_marker_seconds(tree)
        functions = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr in _SUBPROCESS_CALLS:
                if "subprocess" not in ast.unparse(func.value):
                    continue
            elif func.attr not in _BLOCKING_WAIT_CALLS:
                continue
            for keyword in node.keywords:
                if keyword.arg != "timeout":
                    continue
                try:
                    seconds = float(ast.literal_eval(keyword.value))
                except (ValueError, TypeError, SyntaxError):
                    continue
                counts["calls"] += 1
                if seconds < MAX_INNER_OPERATION_SECONDS:
                    continue
                enclosing = [
                    f for f in functions
                    if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)
                ]
                enclosing.sort(key=lambda f: (f.end_lineno or f.lineno) - f.lineno)
                owner = enclosing[0] if enclosing else None
                owner_name = owner.name if owner else "<module>"

                # Scan the whole enclosing chain, innermost outwards, not just
                # the innermost function. A helper NESTED INSIDE a test is
                # covered by that test's marker -- pytest-timeout applies to
                # the call stack, not to a name. Only a helper defined OUTSIDE
                # every test is uncoverable, which is the case the finding
                # message is about. Getting this wrong told a correctly-marked
                # closure to lower a timeout the marker already protected.
                covering_test = next(
                    (f for f in enclosing if f.name.startswith("test_")), None
                )
                owner_is_test = covering_test is not None
                budget = None
                if covering_test is not None:
                    budget = _timeout_marker_seconds(covering_test)
                    owner_name = (
                        owner_name if owner is covering_test
                        else f"{owner_name}() inside {covering_test.name}"
                    )
                if budget is None:
                    budget = module_budget
                if owner_is_test and budget is not None and budget > seconds:
                    continue
                findings.append(
                    _Finding(path, node.lineno, seconds, owner_name,
                             owner_is_test, budget)
                )
    return findings, coverage


# This guard AST-parses the whole backend/testing and scripts corpus (~600
# files); measured at 41s on a Windows checkout against the lanes' own
# --timeout=30. 120s matches the house budget for corpus-scanning guards
# (#17870, test_websocket_emitter_inventory) with ~3x headroom.
@pytest.mark.timeout(120)
def test_no_subprocess_timeout_exceeds_its_lane_budget() -> None:
    """Every wrapped subprocess must be able to hit its own timeout first."""
    findings, coverage = _collect()

    # Non-vacuity: prove the scan actually reached each corpus it can see
    # before trusting a clean result. A guard that scans zero files, or whose
    # AST matcher has stopped recognising the call shape, passes forever.
    assert coverage, (
        f"no test corpus was scanned at all. Looked for backend/testing under "
        f"{BACKEND_TESTING} and scripts under {SCRIPTS_DIR}."
    )
    for root_label, floors in _ROOT_FLOORS.items():
        counts = coverage.get(root_label)
        if counts is None:
            # Both Docker lanes mount the real checkout read-only at /repo, and
            # the CI shards pass CI=true, so a missing root there means a broken
            # mount -- not a layout without that corpus. Refusing to skip in CI
            # is the house convention (test_local_runner_terminal_log.py::
            # _require_local_runner_path). Outside CI, an ad-hoc environment
            # without the checkout may legitimately lack a root.
            if os.getenv("CI"):
                pytest.fail(
                    f"corpus root {root_label!r} was not scanned at all under "
                    f"CI. Looked for backend/testing under {BACKEND_TESTING} "
                    f"and scripts under {SCRIPTS_DIR}. Mount the repo root at "
                    f"/repo so this invariant enforces instead of vanishing."
                )
            continue
        assert counts["files"] >= floors["files"], (
            f"only {counts['files']} test files scanned under {root_label}; "
            f"expected at least {floors['files']}. The guard is not reaching "
            f"the corpus, so a clean result proves nothing."
        )
        assert counts["calls"] >= floors["calls"], (
            f"only {counts['calls']} timeout-bearing blocking calls "
            f"were found under {root_label}; expected at least "
            f"{floors['calls']}. The AST matcher has stopped recognising the "
            f"call shape it is supposed to police."
        )

    assert not findings, (
        f"{len(findings)} blocking call(s) at or above the "
        f"{MAX_INNER_OPERATION_SECONDS}s inner-operation ceiling with no explicit "
        f"@pytest.mark.timeout. An undeclared long call reds as a flake "
        f"rather than a real failure:\n\n"
        + "\n\n".join(str(f) for f in findings)
    )


def test_declared_lane_budget_matches_the_pin() -> None:
    """The pinned literal must still match what CI actually passes.

    Without this, ``LANE_HANG_DETECTOR_SECONDS`` could drift away from the real
    lane budget and the guard above would police a number nothing uses.
    """
    declarations = {
        REPO_ROOT / ".github" / "workflows" / "ci.yml": None,
        REPO_ROOT / "scripts" / "test-backend.sh": None,
    }
    checked = 0
    for path in declarations:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Comment lines are stripped before the scan. Both files are commented
        # in the same syntax they execute, so a `# ... --timeout=300 ...` note
        # explaining the budget would otherwise SATISFY this pin on its own --
        # the vacuous-guard shape where prose, not the executed command, is what
        # the guard reads. Proven by mutation: with comments counted, reverting
        # the real ci.yml invocation to 30 reported `[30, 300]` (the 300 came
        # from a comment); with them stripped the same mutation reports `[30]`.
        executable = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        values = {
            int(m) for m in re.findall(r"--timeout=(\d+)\b", executable)
        }
        if not values:
            continue
        checked += 1
        assert values == {LANE_HANG_DETECTOR_SECONDS}, (
            f"{path.name} passes --timeout={sorted(values)} but this guard is "
            f"pinned to {LANE_HANG_DETECTOR_SECONDS}. Update "
            f"LANE_HANG_DETECTOR_SECONDS deliberately (and re-check every marker "
            f"it governs) rather than letting the pin drift. This is the HANG "
            f"DETECTOR, not MAX_INNER_OPERATION_SECONDS -- never move them "
            f"together."
        )
    if REPO_ROOT.joinpath(".github").is_dir():
        assert checked >= 1, (
            "no lane declared a --timeout= at all; the pin above is now "
            "unanchored to anything CI runs."
        )


def test_the_two_budgets_stay_ordered_and_distinct() -> None:
    """The inner ceiling must stay strictly below the hang detector.

    These are deliberately two numbers doing two jobs. If a later edit collapses
    them -- by lowering the hang detector back onto the ceiling, or by dragging
    the ceiling up with it -- the inversion guard silently becomes the hang
    detector again, which is the exact conflation this split undid. Equality is
    the failure mode, so it is asserted strictly.
    """
    assert MAX_INNER_OPERATION_SECONDS < LANE_HANG_DETECTOR_SECONDS, (
        f"MAX_INNER_OPERATION_SECONDS ({MAX_INNER_OPERATION_SECONDS}) must stay "
        f"strictly below LANE_HANG_DETECTOR_SECONDS "
        f"({LANE_HANG_DETECTOR_SECONDS}). An inner blocking call may never be "
        f"allowed to reach the harness ceiling: at that point the harness kills "
        f"it before its own timeout can report, which is the promotion-killing "
        f"flake this module exists to prevent."
    )
    # The ceiling is the number with teeth; pin it so a bump has to be
    # deliberate and reviewed rather than drifting up with the detector.
    assert MAX_INNER_OPERATION_SECONDS == 30, (
        "MAX_INNER_OPERATION_SECONDS is the budget-inversion ceiling that has "
        "caught a promotion-killing class at least three times. Changing it "
        "needs its own justification, separate from any hang-detector change."
    )


def test_the_guard_rejects_a_known_bad_shape() -> None:
    """Mutation check: the matcher must actually flag the pattern it polices.

    A structural guard that cannot be shown to fail is indistinguishable from
    one that silently matches nothing.
    """
    bad = ast.parse(
        "import subprocess\n"
        "def test_thing():\n"
        f"    subprocess.run(['x'], timeout={MAX_INNER_OPERATION_SECONDS})\n"
    )
    calls = [
        n for n in ast.walk(bad)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _SUBPROCESS_CALLS
    ]
    assert len(calls) == 1
    seconds = float(ast.literal_eval(calls[0].keywords[0].value))
    assert seconds >= MAX_INNER_OPERATION_SECONDS, (
        "the equal-numbers case must be treated as an inversion, not a fit"
    )

    good = ast.parse(
        "import pytest, subprocess\n"
        "@pytest.mark.timeout(120)\n"
        "def test_thing():\n"
        "    subprocess.run(['x'], timeout=60)\n"
    )
    fn = good.body[-1]
    assert _timeout_marker_seconds(fn) == 120.0, (
        "the marker reader must recognise the sanctioned escape hatch"
    )


if __name__ == "__main__":  # pragma: no cover - developer convenience
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# The THIRD budget layer: the scripts test lane's per-FILE budget.
# ---------------------------------------------------------------------------
#
# backend/testing/test_scripts_test_lane.py runs each scripts/test_*.py file as
# ONE subprocess and applies a per-FILE budget to the whole thing. That budget
# is a third layer above the two the guard above reconciles, and the marker
# escape hatch does not reach it: a @pytest.mark.timeout(M) inside a scripts/
# file is honoured by the sharded lane and ignored by the scripts lane, which
# only ever grants the file its per-file budget.
#
# #17887 closed the inner-vs-lane inversion and opened this one in the same
# commit -- it raised scripts/test_github_actions_vm.py to
# @pytest.mark.timeout(120) while the scripts lane still granted the file 25s,
# and CI shard 8 went red with "exceeded the 25-second per-file test budget":
# a healthy file killed by the outer layer, the same class one level further
# out. This section makes that relationship checkable.

_SCRIPTS_LANE = BACKEND_TESTING / "test_scripts_test_lane.py"

# Same grace the lane already uses between its own inner and outer layers, so
# the per-file budget clears the largest marker in the file instead of tying.
_SCRIPTS_LANE_MARKER_GRACE_SECONDS = 60


def _scripts_lane_file_budgets() -> tuple[float, dict[str, float]]:
    """Read the lane's per-file budgets from their owner, via AST not import.

    Importing the lane module would execute its ``_repository_root()``, which
    raises when ``scripts/`` is absent -- a layout this guard legitimately runs
    in. Parsing keeps the guard readable in every layout.
    """
    tree = ast.parse(_SCRIPTS_LANE.read_text(encoding="utf-8"))
    default: float | None = None
    overrides: dict[str, float] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {getattr(target, "id", "") for target in node.targets}
        if "_FILE_BUDGET_SECONDS_DEFAULT" in names:
            default = float(ast.literal_eval(node.value))
        elif "_FILE_BUDGET_SECONDS_OVERRIDES" in names:
            overrides = {
                str(key): float(value)
                for key, value in ast.literal_eval(node.value).items()
            }
    assert default is not None, (
        f"{_SCRIPTS_LANE.name} must declare _FILE_BUDGET_SECONDS_DEFAULT"
    )
    return default, overrides


def _scripts_lane_exempt_files() -> set[str]:
    """Files the scripts lane skips; their per-file budget never applies."""
    harness = SCRIPTS_DIR / "test-backend.sh"
    if not harness.is_file():
        return set()
    source = harness.read_text(encoding="utf-8")
    marker = "CI_SCRIPT_TEST_EXEMPTIONS=("
    start = source.find(marker)
    if start == -1:
        return set()
    end = source.index("\n)", start)
    exempt: set[str] = set()
    for line in source[start + len(marker):end].splitlines():
        entry = line.strip().strip('"').strip()
        if not entry or entry.startswith("#"):
            continue
        exempt.add(entry.partition("|")[0])
    return exempt


def _max_timeout_marker(tree: ast.Module) -> tuple[str, float] | None:
    """Largest ``@pytest.mark.timeout`` in a module, with the owner's name."""
    markers: list[tuple[str, float]] = []
    module_marker = _module_marker_seconds(tree)
    if module_marker is not None:
        markers.append(("pytestmark", module_marker))
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        seconds = _timeout_marker_seconds(node)
        if seconds is not None:
            markers.append((node.name, seconds))
    if not markers:
        return None
    owner, worst = max(markers, key=lambda item: item[1])
    return owner, worst


def test_scripts_lane_file_budgets_clear_their_own_timeout_markers() -> None:
    """A marker inside scripts/ must fit the lane's per-file budget for that file.

    A marker the outer wrapper will not honour is worse than no marker: it
    reads as deliberate headroom while guaranteeing the file dies at the
    smaller number. Either the per-file budget clears the marker with grace, or
    the marker comes down.
    """
    corpus = sorted(SCRIPTS_DIR.glob("test_*.py"))
    if not corpus:
        # Gate on the CORPUS, never on `.is_dir()`: see _scripts_dir(). A
        # layout with no scripts/test_*.py anywhere genuinely has no scripts
        # lane to police -- but only say so when we are not in a checkout,
        # because in a checkout an empty corpus means the glob broke and that
        # must fail loudly instead of skipping.
        assert not _repo_root_is_checkout(), (
            f"no scripts/test_*.py under {SCRIPTS_DIR} despite running from a "
            f"real checkout rooted at {REPO_ROOT}: the corpus glob is broken, "
            f"not absent."
        )
        missing = (
            f"no scripts/ test corpus in this layout (looked under "
            f"{SCRIPTS_DIR} and /repo/scripts)"
        )
        if os.getenv("CI"):
            # House convention: never skip a corpus guard in CI. Every CI lane
            # that runs this file mounts the checkout at /repo, so a miss here
            # is a broken mount, and skipping would retire the guard silently.
            pytest.fail(
                missing + " -- refusing to skip in CI: mount the repo root at "
                "/repo so the scripts lane budgets enforce in the merge gate."
            )
        pytest.skip(missing + ", so the scripts lane does not run here")
    if not _SCRIPTS_LANE.is_file():
        pytest.skip(f"{_SCRIPTS_LANE.name} absent in this layout")

    default_budget, overrides = _scripts_lane_file_budgets()
    exempt = _scripts_lane_exempt_files()

    scanned = 0
    offenders: list[str] = []
    for path in corpus:
        scanned += 1
        if path.name in exempt:
            continue
        worst = _max_timeout_marker(ast.parse(path.read_text(encoding="utf-8")))
        if worst is None:
            continue
        owner, seconds = worst
        budget = overrides.get(path.name, default_budget)
        required = seconds + _SCRIPTS_LANE_MARKER_GRACE_SECONDS
        if budget < required:
            offenders.append(
                f"scripts/{path.name}: {owner} carries "
                f"@pytest.mark.timeout({seconds:g}) but the scripts lane grants "
                f"the whole file only {budget:g}s, so the file is killed at "
                f"{budget:g}s and the marker can never fire. Either raise "
                f'_FILE_BUDGET_SECONDS_OVERRIDES["{path.name}"] in '
                f"backend/testing/test_scripts_test_lane.py to at least "
                f"{required:g} (marker + {_SCRIPTS_LANE_MARKER_GRACE_SECONDS}s "
                f"grace) WITH a comment saying why the work is legitimately "
                f"that slow, or lower the marker."
            )

    # Never pass vacuously (#17872). An empty or unreadable scripts/ corpus
    # would satisfy the offender assertion below without checking anything.
    assert scanned >= _ROOT_FLOORS["scripts"]["files"], (
        f"scanned only {scanned} scripts/test_*.py files against a corpus floor "
        f"of {_ROOT_FLOORS['scripts']['files']}: this guard scanned nothing and "
        f"would have passed regardless of the budgets."
    )
    assert not offenders, "\n".join(
        [
            "scripts-lane per-file budget is below a marker it must honour:",
            *offenders,
        ]
    )


def test_the_scripts_lane_marker_extractor_is_not_vacuous() -> None:
    """The extractor must actually find markers, in both shapes it must handle.

    Without this, a parser that silently returned None for everything would
    make the guard above green forever while checking nothing.
    """
    method_shape = ast.parse(
        "import pytest\n"
        "class T:\n"
        "    @pytest.mark.timeout(120)\n"
        "    def test_x(self):\n"
        "        pass\n"
    )
    assert _max_timeout_marker(method_shape) == ("test_x", 120.0)

    module_shape = ast.parse("import pytest\npytestmark = pytest.mark.timeout(45)\n")
    assert _max_timeout_marker(module_shape) == ("pytestmark", 45.0)

    assert _max_timeout_marker(ast.parse("def test_x():\n    pass\n")) is None


# ---------------------------------------------------------------------------
# SILENT DEATH: pytest exiting non-zero without finishing its session
# ---------------------------------------------------------------------------
#
# A sibling class to the budget inversion above, and the one this module was
# extended for. A budget inversion at least NAMES its victim
# ("Failed: Timeout (>30.0s)"). A pytest process ended by os._exit(), abort(),
# a segfault or the OOM killer names nothing at all: it never reaches
# pytest_sessionfinish, so there is no "short test summary info", no FAILED
# line, and no closing "=== N passed in Xs ===" banner -- just a progress row
# that stops mid-character and a non-zero status.
#
# `Impacted backend tests` went red exactly this way three times on PR #18011
# (runs 32998752057 / 33000772113 / 33002910941). One of those runs had ZERO
# failing tests and still exited 1. The root cause -- the orchestrator hard
# watchdog daemon thread the app lifespan armed and never disarmed, calling
# os._exit(1) ~240s after the last heartbeat -- is fixed in
# app/lifecycle.py::_disarm_orchestrator_hard_watchdog. But the DIAGNOSABILITY
# hole is separate and outlives that one bug: any future in-process abort,
# fatal signal or OOM-kill produces the same unreadable red.
#
# scripts/test-backend.sh::_run_pytest closes it by naming the class when the
# shape appears. These tests hold that helper to its two contracts:
#
#   1. EXIT-CODE FIDELITY. It pipes pytest through `tee`, and `cmd | tee f`
#      yields *tee's* status. This repo has already been burned by that exact
#      masking (`test-backend.sh ... | tail` reporting success while tests
#      failed), so the ${PIPESTATUS[0]} read is load-bearing, not decoration.
#   2. The banner fires on a truncated stream and STAYS QUIET otherwise. A
#      diagnostic that cries wolf on every ordinary red is worse than none.


def _harness_script() -> Path | None:
    """Locate scripts/test-backend.sh, or None when it is not mounted.

    Same trap as _scripts_dir(): in the containerised lanes _repo_root() falls
    back to /app (the bind-mounted backend/), which holds no scripts/test-backend.sh.
    Both Docker lanes also mount the real checkout read-only at /repo, so look
    there before giving up.
    """
    for candidate in (
        REPO_ROOT / "scripts" / "test-backend.sh",
        Path("/repo") / "scripts" / "test-backend.sh",
    ):
        if candidate.is_file():
            return candidate
    return None


HARNESS_SCRIPT = _harness_script()

_NO_HARNESS = pytest.mark.skipif(
    HARNESS_SCRIPT is None,
    reason="scripts/test-backend.sh is not mounted in this lane",
)


def _harness_text() -> str:
    assert HARNESS_SCRIPT is not None
    return HARNESS_SCRIPT.read_text(encoding="utf-8")


def _extract_shell_function(text: str, name: str) -> str:
    """Return the source of a top-level shell function, brace line included.

    Top-level only, and deliberately so: the terminator is a "}" in column 0,
    which is unambiguous in this file (every nested block and the heredoc body
    is indented). A brace-counting parser would have to understand heredocs,
    quoting and case-statement ";;" -- more machinery, more ways to be wrong.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(name + "() {"):
            start = index
            break
    assert start is not None, name + "() not found in scripts/test-backend.sh"
    for index in range(start + 1, len(lines)):
        if lines[index] == "}":
            return "\n".join(lines[start : index + 1]) + "\n"
    raise AssertionError(name + "() has no closing brace in column 0")


def _bash_executable() -> str | None:
    for candidate in (
        Path("C:/Program Files/Git/bin/bash.exe"),
        Path("C:/Program Files/Git/usr/bin/bash.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return shutil.which("bash")


BASH = _bash_executable()

_NO_BASH = pytest.mark.skipif(
    BASH is None, reason="no bash interpreter available to drive the harness helper"
)


def _drive_run_pytest(
    tmp_path: Path,
    *,
    stub_body: str,
    tail: str = "",
    prelude: str = "set -euo pipefail",
) -> "subprocess.CompletedProcess[str]":
    """Run the REAL _run_pytest from test-backend.sh against a stubbed _run.

    The helper is lifted verbatim out of the shipped harness rather than
    re-typed here -- a copy would drift and the guard would then be testing
    itself. ``_run`` is the only seam: stubbing it lets us hand _run_pytest any
    (output, exit status) pair, including the pairs a real pytest can only
    produce by being killed.

    The default ``prelude`` matches the harness's own top-of-file settings
    (test-backend.sh line 20), so the errexit save/restore inside _run_pytest is
    exercised rather than bypassed. Callers override it to prove _run_pytest does
    not silently depend on those settings being what they happen to be today.
    """
    script = tmp_path / "drive.sh"
    script.write_text(
        prelude
        + "\n"
        + _extract_shell_function(_harness_text(), "_run_pytest")
        + "\n_run() {\n"
        + stub_body
        + "\n}\n"
        # `if` is required: under errexit a bare non-zero call aborts the script
        # before we could capture $?. It also proves the caller's errexit is
        # intact on the way out.
        + "if _run_pytest testing/test_example.py; then rc=0; else rc=$?; fi\n"
        + 'echo "RC=$rc"\n'
        + tail,
        encoding="utf-8",
    )
    assert BASH is not None
    return subprocess.run(
        [BASH, str(script)],
        capture_output=True,
        text=True,
        # Below MAX_INNER_OPERATION_SECONDS (30), as this module's own guard
        # requires of a helper: a helper cannot know its callers' budget, so a
        # pytest-timeout marker on it would do nothing. Every stub returns
        # immediately, so 20s can only ever fire on a genuine hang.
        timeout=20,
        cwd=str(tmp_path),
    )


_BANNER = "HARNESS: pytest exited"

# A run ended abruptly part-way through a file -- the exact shape job
# 98289065075 produced on PR #18011: a progress row, no newline, no summary.
_TRUNCATED = (
    "printf '%s' 'testing/test_local_runner.py ....................'\n" "return 1"
)


@_NO_HARNESS
@_NO_BASH
class TestRunPytestSilentDeathDetector:
    """scripts/test-backend.sh::_run_pytest -- exit fidelity and the banner."""

    def test_exit_code_survives_the_tee_pipeline(self, tmp_path: Path) -> None:
        """137 must not be laundered into 0 by tee under the harness's own settings.

        ``tee`` succeeds whenever it can write its file. 137 is chosen because
        it is what Docker surfaces for a SIGKILL/OOM-kill -- the very class this
        helper exists to make legible -- and because it is unmistakably neither
        0 nor 1.
        """
        proc = _drive_run_pytest(
            tmp_path,
            stub_body="printf '%s' 'testing/test_x.py ......'\nreturn 137",
        )
        assert "RC=137" in proc.stdout, (
            "_run_pytest laundered the child's exit status. "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

    def test_exit_code_survives_without_the_caller_s_pipefail(
        self, tmp_path: Path
    ) -> None:
        """THE fidelity check. The test above passes for the WRONG REASON.

        Found by mutation-proving this guard: replacing ${PIPESTATUS[0]} with
        ``$?`` did NOT turn the test above red. With ``set -o pipefail`` in
        effect -- which test-backend.sh happens to set on line 20, and which
        ``set +e`` does NOT clear -- ``$?`` after a pipeline already reports the
        rightmost non-zero status, so the two spellings agree by coincidence.

        That coincidence is the hazard. It makes _run_pytest's correctness
        depend on a shell option set hundreds of lines away, and it makes the
        obvious "simplification" to ``$?`` look safe under test. Drop pipefail --
        as a refactor, a sourcing context or a ``bash -c`` wrapper might --
        and ``$?`` becomes tee's status, which is 0 whenever tee could write its
        file. A killed pytest would then report SUCCESS: the silent-GREEN twin
        of the silent red this helper exists to fix, and the same masking that
        has already burned this repo once (``test-backend.sh ... | tail``).

        So pin the property that actually matters: _run_pytest reports pytest's
        own status regardless of the caller's shell options.
        """
        proc = _drive_run_pytest(
            tmp_path,
            # No pipefail. Everything else as the harness has it.
            prelude="set -eu",
            stub_body="printf '%s' 'testing/test_x.py ......'\nreturn 137",
        )
        assert "RC=137" in proc.stdout, (
            "_run_pytest reported tee's exit status instead of pytest's once the "
            "caller's pipefail was gone -- a killed pytest would read as a pass. "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )

    def test_ordinary_nonzero_exit_is_also_reported_faithfully(
        self, tmp_path: Path
    ) -> None:
        """A normal test failure still returns pytest's own 1."""
        proc = _drive_run_pytest(
            tmp_path,
            stub_body=(
                "echo 'testing/test_x.py ..F..'\n"
                "echo '=== short test summary info ==='\n"
                "echo '=== 1 failed, 4 passed in 3.21s ==='\n"
                "return 1"
            ),
        )
        assert "RC=1" in proc.stdout

    def test_success_is_reported_faithfully(self, tmp_path: Path) -> None:
        proc = _drive_run_pytest(
            tmp_path,
            stub_body=(
                "echo 'testing/test_x.py .....'\n"
                "echo '=== 5 passed in 3.21s ==='\n"
                "return 0"
            ),
        )
        assert "RC=0" in proc.stdout

    def test_truncated_stream_gets_the_banner(self, tmp_path: Path) -> None:
        """The whole point: name the class instead of leaving a bare red."""
        proc = _drive_run_pytest(tmp_path, stub_body=_TRUNCATED)
        assert "RC=1" in proc.stdout
        assert _BANNER in proc.stderr, (
            "a pytest that exited non-zero WITHOUT a session summary produced "
            "no diagnostic -- this is the undiagnosable red of PR #18011. "
            f"stderr={proc.stderr!r}"
        )

    def test_banner_names_the_known_in_process_abort(self, tmp_path: Path) -> None:
        """A banner that only says 'something went wrong' is not a diagnosis.

        The first thing worth checking is a leaked watchdog/supervisor thread,
        so the banner has to name that call site and the grep that confirms it.
        """
        proc = _drive_run_pytest(tmp_path, stub_body=_TRUNCATED)
        assert "_default_hard_abort" in proc.stderr
        assert "HARD-WATCHDOG" in proc.stderr
        assert "--capture=no" in proc.stderr, (
            "the banner must say how to recover the diagnostics pytest's fd "
            "capture swallowed, or the next engineer re-runs just as blind"
        )

    def test_banner_stays_quiet_on_a_finished_failing_session(
        self, tmp_path: Path
    ) -> None:
        """No crying wolf. Ordinary reds must stay unadorned."""
        proc = _drive_run_pytest(
            tmp_path,
            stub_body=(
                "echo 'testing/test_x.py ..F..'\n"
                "echo '=== short test summary info ==='\n"
                "echo 'FAILED testing/test_x.py::test_thing - assert 1 == 2'\n"
                "echo '=== 1 failed, 4 passed in 3.21s ==='\n"
                "return 1"
            ),
        )
        assert "RC=1" in proc.stdout
        assert (
            _BANNER not in proc.stderr
        ), f"banner fired on an ordinary reported failure: {proc.stderr!r}"

    def test_banner_stays_quiet_when_no_tests_ran(self, tmp_path: Path) -> None:
        """Exit 5 with an empty selection is a finished session, not a kill.

        This is the case a naive "did anything FAIL?" detector gets wrong: there
        is no FAILED line and no F, yet pytest concluded normally and said so.
        """
        proc = _drive_run_pytest(
            tmp_path,
            stub_body=("echo '=== no tests ran in 0.04s ==='\n" "return 5"),
        )
        assert "RC=5" in proc.stdout
        assert (
            _BANNER not in proc.stderr
        ), f"banner fired on an empty-but-complete session: {proc.stderr!r}"

    def test_banner_stays_quiet_on_success(self, tmp_path: Path) -> None:
        proc = _drive_run_pytest(
            tmp_path,
            stub_body=("echo '=== 5 passed in 3.21s ==='\n" "return 0"),
        )
        assert _BANNER not in proc.stderr

    def test_caller_errexit_is_restored_after_a_failure(self, tmp_path: Path) -> None:
        """_run_pytest disables errexit around its pipeline. It must put it back.

        If it did not, every command after the first pytest invocation in
        test-backend.sh would stop aborting on failure and the harness would run
        on past a broken step to a green-looking end -- a silent GREEN, which is
        strictly worse than the silent red this helper was added to fix.
        """
        proc = _drive_run_pytest(
            tmp_path,
            stub_body=_TRUNCATED,
            tail="false\necho 'ERREXIT_LOST'\n",
        )
        assert (
            "ERREXIT_LOST" not in proc.stdout
        ), "errexit was not restored: the script ran on past a failing command"
        assert proc.returncode != 0


@_NO_HARNESS
class TestRunPytestWiring:
    """Static wiring checks -- these run even in a lane without bash.

    A perfect _run_pytest that nothing calls detects nothing, so the helper's
    behaviour and its adoption are separate assertions.
    """

    def test_no_pytest_invocation_bypasses_the_helper(self) -> None:
        text = _harness_text()
        raw = [
            line
            for line in text.splitlines()
            if re.search(r"\b_run\s+python3\s+-m\s+pytest\b", line)
        ]
        body = _extract_shell_function(text, "_run_pytest").splitlines()
        bypassing = [line for line in raw if line not in body]
        assert len(raw) == 1 and not bypassing, (
            "every pytest invocation in test-backend.sh must go through "
            "_run_pytest, or that lane keeps producing undiagnosable reds. "
            f"Bypassing lines: {bypassing}"
        )

    def test_helper_is_actually_adopted(self) -> None:
        calls = [
            line
            for line in _harness_text().splitlines()
            if re.match(r"\s*_run_pytest\s", line)
        ]
        assert len(calls) >= 5, (
            "expected _run_pytest at every pytest call site in test-backend.sh "
            f"(standalone, verification, targeted, corpus, full); found {calls}"
        )

    def test_pipestatus_is_read_on_the_line_after_the_pipeline(self) -> None:
        """PIPESTATUS is reset by ANY intervening command, `|| true` included.

        Reading it one line later is the only correct placement, and it is
        exactly the line a well-meaning refactor "tidies" into ``$?``. Pin the
        adjacency, not merely the presence of the word.
        """
        lines = _extract_shell_function(_harness_text(), "_run_pytest").splitlines()
        piped = [i for i, line in enumerate(lines) if "| tee " in line]
        assert len(piped) == 1, f"expected exactly one tee pipeline, found {piped}"
        following = lines[piped[0] + 1].strip()
        assert following == "status=${PIPESTATUS[0]}", (
            "the pipeline's exit status must be captured from PIPESTATUS on the "
            f"immediately following line; found {following!r}. `$?` there would "
            "report tee, which succeeds even when pytest is killed."
        )
