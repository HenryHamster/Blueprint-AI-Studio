"""Recurrence guard: repo ``scripts/`` resolution goes through ONE resolver.

``backend/`` and ``scripts/`` are siblings in a developer checkout, so a fixed
``<backend>/../scripts`` walk resolves there. In the Docker test image every CI
backend shard and ``scripts/test-backend.sh`` use, the backend tree is mounted
at ``/app`` with no ``scripts/`` sibling -- that same walk yields a nonexistent
``/scripts`` -- while the repo root is bind-mounted read-only at ``/repo``. A
hand-rolled walk is therefore silently wrong in exactly one of the two layouts,
and "silently" is the whole problem: the miss lands as a skip, an early return
or a swallowed ``ImportError``, and the test reports coverage it does not have
(the vacuous-guard class, #17872).

``backend/testing/repo_scripts_path.py::resolve_scripts_dir`` is the single
sanctioned resolver. It probes an env override, then ``/repo/scripts``, then the
relative walk, and accepts a candidate only when the directory carries the
``verification_receipt.py`` sentinel -- never on mere directory existence, which
is the same too-weak predicate that made ``.git``-presence gates vacuous. On a
miss it returns a NAMED reason listing every candidate it probed.

This module fails when a NEW hand-rolled copy appears. Two shapes are matched:

* **A parent-walk scripts path** -- ``"scripts"`` appended to an expression that
  walks up a directory tree (``.parent`` / ``.parents[...]`` / ``os.path.dirname``),
  or an ``os.path.join`` carrying both ``".."`` and ``"scripts"``.
* **A ``sys.path`` mutation with a scripts directory** -- ``sys.path.insert`` /
  ``append`` / ``extend`` of anything whose expression mentions "scripts", in a
  module that does not import the shared resolver.

The known-allowed sites are pinned STRUCTURALLY -- by (module, enclosing
qualname, unparsed expression) -- and never by line number. A line-keyed
allowlist rots on the first unrelated edit above it and then either false-fails
or, worse, silently stops covering the site it named; this repo already had to
re-key one for that reason (commit ``993b43535``).

Every allowlist entry must match at least once. A stale entry is a hard failure,
not a shrug: an entry that stops matching means either the site was converged
(delete the entry) or THE MATCHER STOPPED RECOGNISING THE SHAPE IT POLICES, and
a matcher that recognises nothing passes everything.

Standalone -- no database, no network, no ``git``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Corpus. Resolved from THIS FILE, never from the CWD: the standalone lane runs
# from backend/ and the sharded lane from /app, so a CWD-relative root is a
# different (or empty) tree in each. backend/ is self-contained in both
# layouts, so no /repo fallback is needed or wanted here.
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent.parent

# Applied migrations are immutable history and are never edited to fix a path.
_EXCLUDED_PREFIXES = ("alembic/versions/",)

# Literal non-vacuity floor, well below the real count (1230 .py files under
# backend/ excluding alembic/versions as of 2026-08-25). Deliberately NOT
# derived from the scan it polices -- a floor computed from the corpus certifies
# itself and holds for the empty corpus too. Layout-aware: backend/ carries the
# same tree at /app in the Docker image, so one floor is right for both lanes.
_BACKEND_FILE_FLOOR = 900

# Second floor, on the files that survive the "script" prefilter in _scan().
# A prefilter that starts rejecting everything is its own way of making this
# module vacuous, and the file floor above would not notice.
_BACKEND_PARSED_FLOOR = 30

# The canonical resolver must be IN the corpus. If it is not, the root is wrong
# and every "no offender found" answer below is meaningless.
_RESOLVER_MODULE = "testing/repo_scripts_path.py"

_SANCTIONED_IMPORT = "testing.repo_scripts_path"

# Expression text that marks a walk UP a directory tree.
_PARENT_WALK = re.compile(r"\.parent\b|\.parents\s*\[|\bdirname\s*\(")

_SYS_PATH_MUTATORS = ("sys.path.insert", "sys.path.append", "sys.path.extend")

# Source-text prefilters for _scan(). See its docstring for why they exist and
# why they are exact for the two matchers rather than a heuristic narrowing.
_QUOTED_SCRIPTS = re.compile(r"""['"]scripts['"]""")
_PATH_MUTATION = re.compile(r"\.path\.(insert|append|extend)\s*\(")
_MUTATOR_NAMES = frozenset(m.rsplit(".", 1)[1] for m in _SYS_PATH_MUTATORS)


# ---------------------------------------------------------------------------
# Allowlist: (module relpath under backend/, enclosing qualname, unparsed
# expression) -> why this copy is allowed to exist. Structural, not positional.
# ---------------------------------------------------------------------------
ALLOWED_PARENT_WALKS: dict[tuple[str, str, str], str] = {
    (
        "testing/repo_scripts_path.py",
        "resolve_scripts_dir",
        "os.path.join(backend_dir, '..', 'scripts')",
    ): (
        "THE canonical resolver. This is the one authorised copy of the walk; "
        "every other site must call it instead of re-typing it."
    ),
    (
        "scripts/analyze_cochange.py",
        "<module>",
        "Path(__file__).resolve().parents[2] / 'scripts'",
    ): (
        "A __main__-only shim for the changed-file lint helper. It runs as a "
        "standalone script from backend/scripts/ with no `testing` package "
        "importable, so it cannot call the resolver without a layering "
        "inversion. It resolves a path it then execs -- a miss dies loudly in "
        "runpy, it does not degrade into a green test."
    ),
    # ---- backend/scripts/, not the repo scripts/ --------------------------
    # These resolve `backend/scripts/`, a directory that SHIPS INSIDE the
    # backend tree. It is present at <repo>/backend/scripts locally and at
    # /app/scripts in the image, so the fixed walk is correct in both layouts
    # and the two-layout problem this module exists for does not arise.
    (
        "testing/test_cost_attribution.py",
        "_load_cost_reconciliation_module",
        "Path(__file__).parent.parent / 'scripts'",
    ): "resolves backend/scripts/ (ships inside the backend tree)",
    (
        "testing/test_cost_reconciliation.py",
        "<module>",
        "Path(__file__).parent.parent / 'scripts'",
    ): "resolves backend/scripts/ (ships inside the backend tree)",
    (
        "testing/test_measure_repair_success.py",
        "<module>",
        "Path(__file__).parent.parent / 'scripts'",
    ): "resolves backend/scripts/ (ships inside the backend tree)",
    (
        "testing/test_strategic_cycle_cost_projection.py",
        "<module>",
        "Path(__file__).parent.parent / 'scripts'",
    ): "resolves backend/scripts/ (ships inside the backend tree)",
    (
        "testing/test_strategic_effort_tier_evidence.py",
        "<module>",
        "Path(__file__).parent.parent / 'scripts'",
    ): "resolves backend/scripts/ (ships inside the backend tree)",
    # ---- two-candidate walks that already fail loudly ---------------------
    # Each of these pairs the relative walk with the /repo bind-mount and
    # reports a NAMED miss (pytest.fail / AssertionError / FileNotFoundError
    # listing both candidates). They are correct in both layouts today; they
    # are listed rather than converged because each lives in a file with other
    # work in flight, and a needless edit there risks a merge conflict on a
    # promotion PR. They remain owed a convergence pass.
    (
        "testing/test_compute.py",
        "_runner_provision_terminal_reason",
        "Path(__file__).resolve().parents[2] / 'scripts'",
    ): "walk + /repo fallback; a miss raises FileNotFoundError naming the path",
    (
        "testing/test_salvage_bundle_standalone.py",
        "_local_runner_source",
        "Path(__file__).resolve().parents[2] / 'scripts'",
    ): "walk + /repo fallback; a miss raises FileNotFoundError naming both",
    (
        "testing/test_shard_balance.py",
        "_resolve_quality_path",
        "BACKEND_ROOT.parent / 'scripts'",
    ): "walk + /repo fallback; the caller asserts non-None naming both candidates",
    (
        "testing/test_shard_balance.py",
        "test_impute_missing_mean_matches_durations_quality",
        "BACKEND_ROOT.parent / 'scripts'",
    ): "the assertion message for the resolver above; names the candidate probed",
    (
        "testing/test_test_durations_freshness.py",
        "_resolve_quality_path",
        "BACKEND_ROOT.parent / 'scripts'",
    ): "walk + /repo fallback; every caller asserts non-None naming both candidates",
    (
        "testing/test_test_durations_freshness.py",
        "test_outlier_cap_matches_durations_quality",
        "BACKEND_ROOT.parent / 'scripts'",
    ): "assertion message for the resolver above",
    (
        "testing/test_test_durations_freshness.py",
        "test_in_band_setup_thresholds_match_durations_quality",
        "BACKEND_ROOT.parent / 'scripts'",
    ): "assertion message for the resolver above",
    (
        "testing/test_test_durations_freshness.py",
        "test_setup_misattribution_band_is_derived_from_the_observed_plateau",
        "BACKEND_ROOT.parent / 'scripts'",
    ): "assertion message for the resolver above",
    # ---- known-defective, tracked separately ------------------------------
    (
        "testing/test_generated_shell.py",
        "<module>",
        "Path(__file__).resolve().parent.parent.parent / 'scripts'",
    ): (
        "_BLUEPRINT_ROOT's first candidate. This module has a SEPARATE, tracked "
        "defect -- _PACKER_HCL and _AGENT_DIR are built from the raw walk and "
        "never re-pointed at _BLUEPRINT_ROOT, and a total resolution miss leaves "
        "_BLUEPRINT_ROOT at '/' silently. Converging the scripts probe alone "
        "would not fix that, and fixing it is out of scope here."
    ),
}

ALLOWED_SYS_PATH_INSERTS: dict[tuple[str, str, str], str] = {
    (
        "testing/test_scripts_lane_hermetic_credentials.py",
        "_load_scripts_conftest",
        "sys.path.insert(0, str(SCRIPT_ROOT))",
    ): (
        "SCRIPT_ROOT comes from test_scripts_test_lane._repository_root(), a "
        "sentinel-probing repo-root resolver that raises RuntimeError on a miss "
        "-- it never yields a plausible-but-absent path."
    ),
    (
        "testing/test_scripts_lane_hermetic_credentials.py",
        "test_scripts_conftest_defeats_an_ambient_operator_config",
        "sys.path.insert(0, str(SCRIPT_ROOT))",
    ): "same SCRIPT_ROOT as above",
}


# ---------------------------------------------------------------------------
# Matchers. Pure functions over one AST so the self-tests below can drive them
# with synthetic sources instead of trusting the live corpus.
# ---------------------------------------------------------------------------
def _qualname(stack: tuple[str, ...]) -> str:
    return ".".join(stack) if stack else "<module>"


def _string_args(call: ast.Call) -> list[str]:
    return [
        arg.value
        for arg in call.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def find_parent_walk_scripts_paths(tree: ast.AST) -> list[tuple[str, str]]:
    """Every ``"scripts"`` component appended to a directory-tree walk.

    Returns ``(qualname, unparsed_expression)`` pairs.
    """
    findings: list[tuple[str, str]] = []

    def visit(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = (*stack, child.name)
            # Cheap structural tests first: ast.unparse over every node of a
            # 14k-line module is the hot path, and the harness budget for this
            # lane is 60s.
            if (
                isinstance(child, ast.BinOp)
                and isinstance(child.op, ast.Div)
                and isinstance(child.right, ast.Constant)
                and child.right.value == "scripts"
                and _PARENT_WALK.search(ast.unparse(child.left))
            ):
                findings.append((_qualname(stack), ast.unparse(child)))
            elif (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "join"
            ):
                consts = _string_args(child)
                if "scripts" in consts and ast.unparse(child.func).endswith(
                    "path.join"
                ):
                    text = ast.unparse(child)
                    if ".." in consts or _PARENT_WALK.search(text):
                        findings.append((_qualname(stack), text))
            visit(child, inner)

    visit(tree, ())
    return findings


def find_scripts_sys_path_mutations(tree: ast.AST) -> list[tuple[str, str]]:
    """Every ``sys.path`` mutation whose value mentions a scripts directory."""
    findings: list[tuple[str, str]] = []

    def visit(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = (*stack, child.name)
            # `import sys as _sys` is a real spelling in this tree, so match
            # the trailing `...sys.path.<mutator>` shape rather than a literal
            # leading "sys" -- but reach ast.unparse only after the cheap
            # attribute tests have already narrowed it to `<x>.path.<mutator>`.
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _MUTATOR_NAMES
                and isinstance(child.func.value, ast.Attribute)
                and child.func.value.attr == "path"
            ):
                func = ast.unparse(child.func)
                if any(func.endswith(mutator) for mutator in _SYS_PATH_MUTATORS):
                    text = ast.unparse(child)
                    if "script" in text.lower():
                        findings.append((_qualname(stack), text))
            visit(child, inner)

    visit(tree, ())
    return findings


def imports_shared_resolver(tree: ast.AST) -> bool:
    """True when the module imports ``testing.repo_scripts_path``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _SANCTIONED_IMPORT:
            return True
        if isinstance(node, ast.Import) and any(
            alias.name == _SANCTIONED_IMPORT for alias in node.names
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Corpus scan
# ---------------------------------------------------------------------------
Finding = tuple[str, str, str]


def _iter_backend_modules() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(BACKEND_DIR.rglob("*.py")):
        rel = path.relative_to(BACKEND_DIR).as_posix()
        if rel.startswith(_EXCLUDED_PREFIXES):
            continue
        found.append((rel, path))
    return found


def _scan() -> tuple[list[Finding], list[Finding], int, int]:
    """Return (parent_walks, sys_path_mutations, files_read, files_parsed).

    Each finding is ``(module_relpath, qualname, unparsed_expression)``.

    Every file is READ; only files that could possibly yield a finding are
    PARSED. The prefilter is derived from what the matchers require, not
    guessed at: matcher A needs a quoted ``scripts`` path component, and
    matcher B needs both a ``.path.insert/append/extend(`` call and the
    substring ``script``. It exists because backend/ is ~61 MB of Python, the
    full ast.parse of it takes ~45s on a developer machine, and this lane runs
    under a 60s harness budget -- an inner cost that meets the outer budget is
    its own promotion-killing flake class.
    """
    walks: list[Finding] = []
    inserts: list[Finding] = []
    read = 0
    parsed = 0
    for rel, path in _iter_backend_modules():
        source = path.read_text(encoding="utf-8", errors="replace")
        read += 1
        if not _QUOTED_SCRIPTS.search(source) and not (
            _PATH_MUTATION.search(source) and "script" in source.lower()
        ):
            continue
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            pytest.fail(f"{rel} does not parse, so it cannot be checked: {exc}")
        parsed += 1
        for qualname, text in find_parent_walk_scripts_paths(tree):
            walks.append((rel, qualname, text))
        if not imports_shared_resolver(tree):
            for qualname, text in find_scripts_sys_path_mutations(tree):
                inserts.append((rel, qualname, text))
    return walks, inserts, read, parsed


_SCANNED: tuple[list[Finding], list[Finding], int, int] | None = None


def _scan_once() -> tuple[list[Finding], list[Finding], int, int]:
    global _SCANNED
    if _SCANNED is None:
        _SCANNED = _scan()
    return _SCANNED


def _fix_instruction(kind: str) -> str:
    return (
        f"\n\nA new hand-rolled {kind} bypasses "
        "backend/testing/repo_scripts_path.py::resolve_scripts_dir. Call the "
        "shared resolver instead:\n"
        "    from testing.repo_scripts_path import resolve_scripts_dir_from_env\n"
        "    _BACKEND_DIR = os.path.normpath("
        "os.path.join(os.path.dirname(__file__), '..'))\n"
        "    scripts_dir, unavailable = resolve_scripts_dir_from_env(_BACKEND_DIR)\n"
        "and FAIL with `unavailable` on a miss -- never pytest.skip it, never "
        "swallow the ImportError, never fall back to a plausible path. If the "
        "site genuinely must keep its own walk, add it to the allowlist in "
        f"{Path(__file__).name} together with the reason."
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------
def test_the_scan_reaches_a_real_backend_tree() -> None:
    """Non-vacuity: a mis-resolved root finds zero offenders and passes."""
    _, _, read, parsed = _scan_once()
    assert read >= _BACKEND_FILE_FLOOR, (
        f"read only {read} python files under {BACKEND_DIR} against a floor "
        f"of {_BACKEND_FILE_FLOOR}. This guard read almost nothing and would "
        f"have reported 'no offenders' regardless of the source. Check that "
        f"{BACKEND_DIR} is the real backend tree."
    )
    assert parsed >= _BACKEND_PARSED_FLOOR, (
        f"only {parsed} of {read} files survived the source prefilter, "
        f"against a floor of {_BACKEND_PARSED_FLOOR}. The prefilter is now "
        f"eating the corpus, so the matchers below never see the sites they "
        f"police."
    )
    modules = {rel for rel, _ in _iter_backend_modules()}
    assert _RESOLVER_MODULE in modules, (
        f"{_RESOLVER_MODULE} is not in the scanned corpus under {BACKEND_DIR}. "
        "The canonical resolver must be inside the tree this guard polices, or "
        "the corpus root is wrong and every result below is meaningless."
    )


def test_no_unallowed_parent_walk_resolves_the_scripts_directory() -> None:
    walks, _, _, _ = _scan_once()
    offenders = [finding for finding in walks if finding not in ALLOWED_PARENT_WALKS]
    assert not offenders, (
        "hand-rolled repo scripts/ walks outside the allowlist:\n"
        + "\n".join(f"  {rel} :: {qual} :: {text}" for rel, qual, text in offenders)
        + _fix_instruction("<something>/../scripts walk")
    )


def test_no_unallowed_sys_path_insert_adds_a_scripts_directory() -> None:
    _, inserts, _, _ = _scan_once()
    offenders = [
        finding for finding in inserts if finding not in ALLOWED_SYS_PATH_INSERTS
    ]
    assert not offenders, (
        "sys.path mutations adding a scripts directory in modules that do not "
        f"import {_SANCTIONED_IMPORT}:\n"
        + "\n".join(f"  {rel} :: {qual} :: {text}" for rel, qual, text in offenders)
        + _fix_instruction("sys.path insert of a scripts directory")
    )


def test_every_allowlist_entry_still_matches_a_real_site() -> None:
    """A stale entry means the site converged OR the matcher went blind.

    Either way the allowlist is lying about what it covers, and a matcher that
    recognises nothing passes everything -- the exact failure this module
    exists to prevent.
    """
    walks, inserts, _, _ = _scan_once()
    stale_walks = sorted(set(ALLOWED_PARENT_WALKS) - set(walks))
    stale_inserts = sorted(set(ALLOWED_SYS_PATH_INSERTS) - set(inserts))
    assert not stale_walks and not stale_inserts, (
        "allowlist entries that no longer match any site:\n"
        + "\n".join(
            f"  [walk] {rel} :: {qual} :: {text}" for rel, qual, text in stale_walks
        )
        + "\n".join(
            f"  [sys.path] {rel} :: {qual} :: {text}"
            for rel, qual, text in stale_inserts
        )
        + "\n\nIf the site was converged onto resolve_scripts_dir, delete its "
        "entry. If it was NOT, the matcher has stopped recognising the shape it "
        "polices and this whole module is now vacuous -- fix the matcher."
    )


def test_the_named_conversion_targets_use_the_shared_resolver() -> None:
    """Literal pins for the sites #17910 was cut to fix.

    Named explicitly so a future refactor that drops the import cannot pass
    merely because the generic scan found nothing to flag.
    """
    pinned = (
        "testing/test_publication_gate_receipt.py",
        "testing/test_runner_progress_protocol.py",
        "testing/test_compute_watchdog.py",
        "testing/test_deployed_sha.py",
        "testing/test_check_gate_fail_closed.py",
    )
    missing = []
    for rel in pinned:
        path = BACKEND_DIR / rel
        assert path.is_file(), f"{rel} is missing from {BACKEND_DIR}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        if not imports_shared_resolver(tree):
            missing.append(rel)
    assert not missing, (
        f"these modules must resolve scripts/ through {_SANCTIONED_IMPORT}: "
        + ", ".join(missing)
    )


# ---------------------------------------------------------------------------
# Matcher self-tests. The corpus assertions above are only as good as these.
# ---------------------------------------------------------------------------
_BAD_WALKS = (
    "import os\nP = os.path.join(os.path.dirname(__file__), '..', 'scripts')\n",
    "from pathlib import Path\nP = Path(__file__).resolve().parents[2] / 'scripts'\n",
    "from pathlib import Path\nP = Path(__file__).parent.parent / 'scripts'\n",
    "def f():\n    return ROOT.parent / 'scripts'\n",
    "import os\nP = os.path.join(BACKEND, '..', 'scripts', 'local_runner.py')\n",
)

_GOOD_WALKS = (
    # An already-resolved root, not a walk.
    "P = REPO_ROOT / 'scripts'\n",
    # A synthetic tree built by a fixture.
    "def test_x(tmp_path):\n    (tmp_path / 'scripts').mkdir()\n",
    # The container mount, spelled absolutely.
    "from pathlib import Path\nP = Path('/repo') / 'scripts'\n",
    # A probe loop over candidate roots.
    "for candidate in roots:\n    if (candidate / 'scripts').is_dir():\n        pass\n",
    # A different directory entirely.
    "from pathlib import Path\nP = Path(__file__).parent.parent / 'docs'\n",
)


@pytest.mark.parametrize("source", _BAD_WALKS)
def test_the_walk_matcher_flags_each_known_bad_shape(source: str) -> None:
    assert find_parent_walk_scripts_paths(
        ast.parse(source)
    ), f"the parent-walk matcher no longer recognises:\n{source}"


@pytest.mark.parametrize("source", _GOOD_WALKS)
def test_the_walk_matcher_leaves_the_sanctioned_shapes_alone(source: str) -> None:
    assert not find_parent_walk_scripts_paths(
        ast.parse(source)
    ), f"the parent-walk matcher false-flags:\n{source}"


_BAD_INSERTS = (
    "import sys\nsys.path.insert(0, _SCRIPTS_DIR)\n",
    "import sys\nsys.path.append(str(scripts_root))\n",
    "import sys as _sys\n_sys.path.insert(0, _SCRIPTS_DIR)\n",
    "def f():\n    sys.path.insert(0, '/repo/scripts')\n",
)

_GOOD_INSERTS = (
    "import sys\nsys.path.insert(0, _BACKEND_DIR)\n",
    "import sys\nsys.path.insert(0, str(tmp_path))\n",
    "scripts_dir = resolve()\n",
)


@pytest.mark.parametrize("source", _BAD_INSERTS)
def test_the_sys_path_matcher_flags_each_known_bad_shape(source: str) -> None:
    assert find_scripts_sys_path_mutations(
        ast.parse(source)
    ), f"the sys.path matcher no longer recognises:\n{source}"


@pytest.mark.parametrize("source", _GOOD_INSERTS)
def test_the_sys_path_matcher_leaves_the_sanctioned_shapes_alone(source: str) -> None:
    assert not find_scripts_sys_path_mutations(
        ast.parse(source)
    ), f"the sys.path matcher false-flags:\n{source}"


@pytest.mark.parametrize(
    "source,expected",
    [
        ("from testing.repo_scripts_path import resolve_scripts_dir\n", True),
        (
            "from testing.repo_scripts_path import (\n    resolve_scripts_dir,\n)\n",
            True,
        ),
        ("import testing.repo_scripts_path\n", True),
        ("from testing.identity_extraction import x\n", False),
        ("from repo_scripts_path import resolve_scripts_dir\n", False),
    ],
)
def test_the_resolver_import_detector_is_exact(source: str, expected: bool) -> None:
    assert imports_shared_resolver(ast.parse(source)) is expected


def test_a_planted_offender_is_caught_end_to_end(tmp_path) -> None:
    """Mutation proof, re-run on every invocation.

    The corpus assertions above prove nothing on their own -- a matcher that
    always returns [] makes them all pass. This drives the same two matchers
    over a file that would be an offence if it were committed.
    """
    offender = tmp_path / "test_new_hand_rolled_copy.py"
    offender.write_text(
        "import os\n"
        "import sys\n"
        "_SCRIPTS = os.path.normpath(\n"
        "    os.path.join(os.path.dirname(__file__), '..', 'scripts')\n"
        ")\n"
        "sys.path.insert(0, _SCRIPTS)\n"
        "try:\n"
        "    import verification_receipt\n"
        "except ImportError:\n"
        "    verification_receipt = None\n",
        encoding="utf-8",
    )
    tree = ast.parse(offender.read_text(encoding="utf-8"))
    assert find_parent_walk_scripts_paths(tree) == [
        ("<module>", "os.path.join(os.path.dirname(__file__), '..', 'scripts')")
    ]
    assert find_scripts_sys_path_mutations(tree) == [
        ("<module>", "sys.path.insert(0, _SCRIPTS)")
    ]
    assert not imports_shared_resolver(tree)
