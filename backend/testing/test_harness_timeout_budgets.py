"""
Standalone guard: no test may wrap a subprocess in a timeout that is >= the
pytest-timeout budget the CI lane runs it under.

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
import re
from pathlib import Path

import pytest

# The pytest-timeout budget every backend CI lane passes on the command line.
#
# Pinned as a literal ON PURPOSE. Reading it out of the config files would make
# this guard self-certifying: bumping ci.yml would silently relax every
# assertion below. Instead the literal is the authority and
# ``test_declared_lane_budget_matches_the_pin`` proves the config still agrees
# with it, so a deliberate bump has to touch this line and be reviewed.
LANE_TIMEOUT_SECONDS = 30

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
    at "." scans a different (or empty) tree in each.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "backend" / "testing").is_dir():
            return candidate
    # Docker-only layout: the image carries backend/ as /app with no repo root.
    return here.parents[1]


REPO_ROOT = _repo_root()
BACKEND_TESTING = REPO_ROOT / "backend" / "testing"
if not BACKEND_TESTING.is_dir():
    BACKEND_TESTING = Path(__file__).resolve().parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


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
                f"in timeout={self.seconds:g}s, at or above the {LANE_TIMEOUT_SECONDS}s "
                f"lane budget. A helper cannot carry a pytest-timeout marker that "
                f"applies to its callers -- lower the inner timeout instead."
            )
        if self.budget is None:
            return (
                f"{rel}:{self.lineno}: {self.owner}() wraps a subprocess in "
                f"timeout={self.seconds:g}s under a {LANE_TIMEOUT_SECONDS}s lane budget "
                f"with no @pytest.mark.timeout. The harness kills the call before "
                f"its own timeout can fire."
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
                if seconds < LANE_TIMEOUT_SECONDS:
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
            # scripts/ is genuinely absent inside the backend Docker image; the
            # standalone lane covers it from the checkout.
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
        f"{len(findings)} subprocess timeout(s) at or above the "
        f"{LANE_TIMEOUT_SECONDS}s CI lane budget. The harness kills the call "
        f"before its own timeout fires, so it reds as an unreproducible flake "
        f"rather than a real failure:\n\n"
        + "\n\n".join(str(f) for f in findings)
    )


def test_declared_lane_budget_matches_the_pin() -> None:
    """The pinned literal must still match what CI actually passes.

    Without this, ``LANE_TIMEOUT_SECONDS`` could drift away from the real lane
    budget and the guard above would police a number nothing uses.
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
        values = {
            int(m) for m in re.findall(r"--timeout=(\d+)\b", text)
        }
        if not values:
            continue
        checked += 1
        assert values == {LANE_TIMEOUT_SECONDS}, (
            f"{path.name} passes --timeout={sorted(values)} but this guard is "
            f"pinned to {LANE_TIMEOUT_SECONDS}. Update LANE_TIMEOUT_SECONDS "
            f"deliberately (and re-check every marker it governs) rather than "
            f"letting the pin drift."
        )
    if REPO_ROOT.joinpath(".github").is_dir():
        assert checked >= 1, (
            "no lane declared a --timeout= at all; the pin above is now "
            "unanchored to anything CI runs."
        )


def test_the_guard_rejects_a_known_bad_shape() -> None:
    """Mutation check: the matcher must actually flag the pattern it polices.

    A structural guard that cannot be shown to fail is indistinguishable from
    one that silently matches nothing.
    """
    bad = ast.parse(
        "import subprocess\n"
        "def test_thing():\n"
        f"    subprocess.run(['x'], timeout={LANE_TIMEOUT_SECONDS})\n"
    )
    calls = [
        n for n in ast.walk(bad)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _SUBPROCESS_CALLS
    ]
    assert len(calls) == 1
    seconds = float(ast.literal_eval(calls[0].keywords[0].value))
    assert seconds >= LANE_TIMEOUT_SECONDS, (
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
    if not SCRIPTS_DIR.is_dir():
        pytest.skip(
            "scripts/ absent in this layout: the Docker test image carries "
            "backend/ as /app only, so the scripts lane does not run here"
        )
    if not _SCRIPTS_LANE.is_file():
        pytest.skip(f"{_SCRIPTS_LANE.name} absent in this layout")

    default_budget, overrides = _scripts_lane_file_budgets()
    exempt = _scripts_lane_exempt_files()

    scanned = 0
    offenders: list[str] = []
    for path in sorted(SCRIPTS_DIR.glob("test_*.py")):
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
