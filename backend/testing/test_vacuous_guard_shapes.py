"""Standalone guard: no guard may be written in a shape that cannot fail.

SCOPE: every ``*.py`` module under ``backend/testing`` and ``scripts`` -- test
modules AND the helper/checker modules they delegate to. The rules below are
about guard machinery, and in this repository the machinery lives mostly in
modules the ``test_`` prefix does not match. See ``_iter_corpus_files``.


THE DEFECT CLASS THIS EXISTS TO PREVENT
---------------------------------------
A *vacuous guard* is a test that appears to enforce a property but structurally
cannot fail, so it reports green forever while enforcing nothing. It is worse
than no test: the lane reports coverage that does not exist, and a green local
run of the file proves nothing at all about the invariant it names.

Every instance shares one root shape -- **a precondition that silently
evaluates to "nothing to check" instead of failing loudly.** Confirmed
instances, all measured rather than guessed:

  * ``test_salvage_bundle_standalone.test_m2_all_alert_constructors_have_event_type``
    globbed the CWD-relative pattern ``backend/app/**/*.py``. Every lane that
    runs it already has backend/ as its CWD (``/app`` inside the test image),
    so it resolved to ``backend/backend/app/**``. Instrumented in the
    standalone lane: **0 files scanned**, against 571 real ones. It had never
    been capable of failing (issue #17872).

  * ``test_size_offender_governance._real_repo_checkout`` decided whether
    the cap-bump ledger guard ran from the PRESENCE of ``.git``. Both spellings
    of that are wrong, and the repo shipped both. ``os.path.isdir`` was the
    original: in a LINKED WORKTREE ``.git`` is a FILE holding ``gitdir: ...``,
    so the guard silently skipped in every agent worktree and every dev
    worktree and only ever ran in a real clone (#17887). ``os.path.exists``
    fixed that and OVERSHOT: a worktree's ``gitdir:`` line names a HOST path
    that does not exist inside a container, so git died with ``fatal: not a git
    repository: /repo/D:/.../.git/worktrees/<name>`` and the guard ERRORED
    instead of skipping -- measured ``1 failed, 120 passed`` (#17892).

  * ``test_project_tools.test_asset_serving_blocks_path_traversal`` wrapped its
    call in ``try: ... assert False, "expected rejection" ... except
    Exception: pass``. ``assert False`` raises ``AssertionError``, which IS an
    ``Exception`` -- so the handler returning normally, with no rejection at
    all, was swallowed into a pass. A path-traversal guard that could not fail.

THE RULES
---------
1. A corpus scan may not be rooted at a bare CWD-relative string literal.
   Resolve from ``Path(__file__).resolve()``; the lanes run from different
   working directories and one of them will scan an empty tree.
2. Whether a git-backed guard runs may not be decided from the PRESENCE of
   ``.git``, in either spelling and in either direction -- neither skipping on
   its absence nor ACCEPTING a root on its presence. ``isdir`` under-shoots
   (skips in every worktree); ``exists`` over-shoots (errors in a
   container-mounted worktree). Ask git the cheapest question that fails in
   exactly the cases the guard's real git call would -- ``git rev-parse HEAD``
   -- via ``testing.git_checkout_probe.git_readable_repo_root``.
3. A ``try`` block may not swallow its own failure signal. If the body raises
   ``AssertionError`` (via ``assert``, ``pytest.fail``, ``self.fail``) and a
   handler catches ``Exception``/``BaseException``/``AssertionError``/bare and
   swallows it, the test passes no matter what the code under test does.

Runs under --noconftest (no DB, no conftest fixtures).
"""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from testing.git_checkout_probe import git_readable_repo_root
from testing.repo_scripts_path import resolve_scripts_dir_from_env

# A string literal that is exactly ``.git``. Deliberately NOT a substring test
# for ".git": that also matches ".github", and a ``.github`` directory check is
# a perfectly correct isdir.
_GIT_PATH_LITERAL = re.compile(r"""['"]\.git['"]""")

# Every filesystem predicate that can stand in for "is there a checkout here".
# ALL of them are wrong as the deciding question -- see rule 2 above -- so the
# matcher deliberately does not distinguish is_dir from exists.
_FS_PRESENCE_PREDICATES = frozenset(
    {"is_dir", "isdir", "exists", "is_file", "isfile"}
)

# Statement shapes that turn a guard off. An ``if`` on a ``.git`` predicate
# whose body is only these is deciding executability from presence.
_SKIP_CALLS = frozenset({"skip", "skipTest", "xfail"})

# Calls that materialise a file corpus. A CWD-relative literal root here is the
# #17872 shape exactly.
_CORPUS_SCAN_CALLS = frozenset({"glob", "iglob", "listdir", "walk"})

# Handlers broad enough to catch the test's own AssertionError.
_ASSERTION_CATCHING = frozenset({"Exception", "BaseException", "AssertionError"})

# Top-level repository directories. A literal starting with one of these is a
# repo path being resolved against the CWD, not an incidental relative name.
_REPO_TOPLEVEL = (
    "backend/", "agent/", "admin-ui/", "scripts/", "infra/", "docs/",
    ".github/", "backend\\", "agent\\", "scripts\\", "infra\\",
)

# Non-vacuity floors. Pinned as literals ON PURPOSE and set far below the real
# counts (662 files / 22 of them non-test / 407 predicates / 296 corpus calls /
# 1113 try blocks under backend/testing, 105 / 62 / 290 / 56 / 835 under scripts
# as of 2026-08-25) so a broken path -- or a matcher that has stopped
# recognising the shape it polices -- fails loudly instead of passing on an
# empty corpus. Deriving them from the scan itself is exactly the
# self-certification this module exists to prevent.
#
# ``non_test_files`` is the floor that keeps the corpus from silently shrinking
# back to ``test_*.py``. That narrowing is not hypothetical: it is what this
# module shipped with, and it left every guard IMPLEMENTATION -- the modules
# that actually root corpus scans and probe ``.git`` -- outside the scan. A
# planted violation of each rule sat in two of them while this module reported
# ``6 passed``. A ``files`` floor alone cannot catch that: backend/testing has
# 640 test modules, so dropping all 22 non-test ones barely moves it.
#
# They are declared PER ROOT because the lanes see different trees: the sharded
# backend lane runs inside the Docker image, which carries backend/ as /app and
# has no scripts/ of its own (it reaches it through the /repo bind mount), while
# a plain checkout sees both directly. A single combined floor would either be
# too low to mean anything or would false-fail inside the image.
_ROOT_FLOORS = {
    "backend/testing": {
        "files": 400,
        "non_test_files": 15,
        "fs_predicates": 200,
        "corpus_calls": 150,
        "try_blocks": 600,
    },
    "scripts": {
        "files": 25,
        "non_test_files": 40,
        "fs_predicates": 80,
        "corpus_calls": 12,
        "try_blocks": 60,
    },
}


def _repo_root() -> Path:
    """Resolve the checkout root from this file, never from the CWD.

    CWD-relative discovery is how a guard becomes vacuous in the first place:
    the standalone lane runs from ``backend/`` and the sharded lane from
    ``/app``, so a root of "." is a different (or empty) tree in each.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "backend" / "testing").is_dir():
            return candidate
    # Docker-only layout: the image carries backend/ as /app, with no repo root
    # above it.
    return here.parents[1]


REPO_ROOT = _repo_root()

BACKEND_TESTING = REPO_ROOT / "backend" / "testing"
if not BACKEND_TESTING.is_dir():
    BACKEND_TESTING = Path(__file__).resolve().parent

# Both CI lanes bind-mount the repository root read-only at /repo, so the
# scripts corpus IS reachable from inside the image even though it is not below
# backend/.
#
# Resolution is DELEGATED to testing.repo_scripts_path rather than open-coded,
# because the obvious open-coding is wrong in a way nothing reports. In the
# shard image the backend is mounted at /app with no repo root above it, so
# REPO_ROOT degrades to /app and `REPO_ROOT / "scripts"` names *backend/scripts*
# -- a real directory holding zero test_*.py. An is_dir() probe is satisfied by
# it, the glob returns nothing, and the scripts corpus is silently never
# scanned: this module's own defect class, inside this module. The shared
# resolver probes /repo first and keys on a sentinel FILE, so a directory that
# merely exists can never answer for the corpus, and it hands back a reason
# naming every candidate it tried.
_SCRIPTS_DIR_TEXT, _SCRIPTS_DIR_UNAVAILABLE = resolve_scripts_dir_from_env(
    str(Path(__file__).resolve().parent.parent)
)
SCRIPTS_DIR = Path(_SCRIPTS_DIR_TEXT) if _SCRIPTS_DIR_TEXT else None


# Label -> why a scan of that root could have come back empty, so a coverage
# miss names the tree it failed to reach instead of reporting a bare label.
_ROOT_MISS_REASONS = {
    "backend/testing": f"it resolved to {BACKEND_TESTING}",
    "scripts": _SCRIPTS_DIR_UNAVAILABLE or f"it resolved to {SCRIPTS_DIR}",
}


def _iter_corpus_files() -> list[tuple[str, Path]]:
    """Yield (root_label, path) for EVERY ``*.py`` module in each guard root.

    Not ``test_*.py``. The three rules police guard MACHINERY -- where a corpus
    scan is rooted, how a ``.git``-backed guard decides it can run, whether a
    handler eats its own failure signal -- and in this repository most of that
    machinery lives in helper modules whose names the ``test_`` prefix does not
    match: ``check_size_limits.py``, ``check_test_imports.py``,
    ``impacted_tests.py``, ``estate_inventory.py``, ``git_checkout_probe.py``
    and ``repo_scripts_path.py`` under backend/testing, and
    ``static_release_guard.py``, ``sentinel_freshness.py``,
    ``backend_verification_guard.py`` and ``durations_quality.py`` under
    scripts/. A ``test_*.py``-only corpus left 84 of those modules -- 22 under
    backend/testing and 62 under scripts -- permanently unscanned, which is
    this module's own defect class inside this module: the meta-guard reported
    clean over the test files while every guard IMPLEMENTATION sat outside its
    reach.

    MEASURED, not argued: one violation of each of the three rules was planted
    in ``estate_inventory.py`` and ``sentinel_freshness.py`` and this module
    still reported ``6 passed``. ``test_the_corpus_reaches_non_test_modules``
    below, and the ``non_test_files`` floors in ``_ROOT_FLOORS``, exist so a
    future narrowing of this glob fails loudly instead of silently shrinking
    the corpus back.
    """
    found: list[tuple[str, Path]] = [
        ("backend/testing", p) for p in sorted(BACKEND_TESTING.glob("*.py"))
    ]
    if SCRIPTS_DIR is not None:
        found.extend(("scripts", p) for p in sorted(SCRIPTS_DIR.glob("*.py")))
    return found


def _is_test_module(path: Path) -> bool:
    return path.name.startswith("test_")


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


# ---------------------------------------------------------------------------
# Rule matchers. Each is a pure function over one AST so the self-tests below
# can drive it with synthetic sources.
# ---------------------------------------------------------------------------


def _dot_git_presence_predicates(scope: ast.AST) -> list[ast.Call]:
    """Every filesystem predicate in ``scope`` applied to a ``.git`` path."""
    hits: list[ast.Call] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) not in _FS_PRESENCE_PREDICATES:
            continue
        # os.path.isdir(<arg>)  /  <receiver>.exists()
        subjects = [ast.unparse(a) for a in node.args]
        if isinstance(node.func, ast.Attribute):
            subjects.append(ast.unparse(node.func.value))
        if any(_GIT_PATH_LITERAL.search(text) for text in subjects):
            hits.append(node)
    return hits


def _branch_effect(body: list[ast.stmt]) -> str | None:
    """Classify a branch body as "skip", "reject", "accept", or neither.

    * ``skip`` -- the branch declares the guard did not run. Nothing downstream
      can recover from it, so a ``.git`` predicate deciding it IS the gate.
    * ``reject`` -- the branch abandons one candidate (``return None``/``False``).
    * ``accept`` -- the branch ANSWERS the availability question affirmatively
      (``return root`` / ``return True``). This is the shape the first draft of
      this rule missed, and it is the one the module docstring is about:
      ``test_size_offender_governance._real_repo_checkout`` was
      ``for root in candidates: if os.path.exists(root/".git"): return root``,
      and both the ``isdir`` and the ``exists`` spelling of that were wrong.

    ``reject`` and ``accept`` are defects only when nothing in the enclosing
    function goes on to ask git. Inside ``testing.git_checkout_probe`` the same
    two shapes are the sanctioned cheap pre-filter ahead of the real probe, and
    git -- not the filesystem -- gives the final answer there.
    """
    effect: str | None = None
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and _call_name(stmt.value) in _SKIP_CALLS
        ):
            return "skip"
        if (
            isinstance(stmt, ast.Raise)
            and stmt.exc is not None
            and "Skipped" in ast.unparse(stmt.exc)
        ):
            return "skip"
        if isinstance(stmt, ast.Return):
            value = stmt.value
            if value is None or (
                isinstance(value, ast.Constant) and value.value in (None, False)
            ):
                effect = "reject"
            else:
                effect = "accept"
            continue
        return None
    return effect




_RULE_2_VERBS = {
    "skip": "skips this guard",
    "reject": "abandons the candidate, and nothing here asks git",
    "accept": (
        "answers the availability question from the filesystem, and nothing "
        "here asks git"
    ),
}


def _invokes_git(scope: ast.AST) -> bool:
    """True when this scope actually runs git, rather than only looking for it."""
    return any(
        isinstance(node, ast.Constant) and node.value == "git"
        for node in ast.walk(scope)
    )


def find_dot_git_presence_gates(tree: ast.AST) -> list[tuple[int, str]]:
    """Flag guards whose executability is decided by the presence of ``.git``.

    Three shapes, all measured against the real corpus:

    * an ``if`` on a ``.git`` predicate that SKIPS -- the inline gate;
    * an ``if`` on a ``.git`` predicate that rejects the candidate inside a
      function which never runs git;
    * a helper whose RETURN VALUE is a ``.git`` predicate and which never runs
      git -- the "is a checkout available" boolean.

    A ``.git`` predicate used as a cheap pre-filter *ahead of* a real git call
    is deliberately NOT flagged: that is the sanctioned shape in
    ``testing.git_checkout_probe.git_readable_repo_root``, where git, not the
    filesystem, gives the answer.
    """
    findings: list[tuple[int, str]] = []

    def visit(node: ast.AST, invokes_git: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_runs_git = _invokes_git(child)
                if not child_runs_git:
                    for stmt in ast.walk(child):
                        if not isinstance(stmt, ast.Return) or stmt.value is None:
                            continue
                        for call in _dot_git_presence_predicates(stmt.value):
                            findings.append(
                                (
                                    stmt.lineno,
                                    f"`{child.name}()` returns "
                                    f"{ast.unparse(call)} as its availability "
                                    f"answer without ever running git",
                                )
                            )
                            break
                visit(child, child_runs_git)
                continue
            if isinstance(child, ast.If):
                effect = _branch_effect(child.body)
                gated = effect in ("reject", "accept") and invokes_git
                if effect is not None and not gated:
                    for call in _dot_git_presence_predicates(child.test):
                        verb = _RULE_2_VERBS[effect]
                        findings.append(
                            (child.lineno, f"`if ... {ast.unparse(call)} ...` {verb}")
                        )
                        break
            visit(child, invokes_git)

    visit(tree, False)
    return findings


def _cwd_relative_scan(node: ast.Call) -> str | None:
    """Detail string when this call scans a bare CWD-relative repo path."""
    if _call_name(node) not in _CORPUS_SCAN_CALLS:
        return None
    if not node.args:
        return None
    first = node.args[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None
    value = first.value
    if not value or value.startswith(("/", "\\")):
        return None
    if len(value) > 1 and value[1] == ":":  # C:\... on Windows
        return None
    if not value.startswith(_REPO_TOPLEVEL):
        return None
    return f"{_call_name(node)}({value!r})"


def find_cwd_relative_corpus_scans(tree: ast.AST) -> list[tuple[int, str]]:
    """Flag every corpus scan in ``tree`` rooted at a CWD-relative repo path."""
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            detail = _cwd_relative_scan(node)
            if detail is not None:
                findings.append((node.lineno, detail))
    return findings


def _handler_catches_assertion(handler: ast.ExceptHandler) -> str | None:
    if handler.type is None:
        return "bare except"
    exprs = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    caught = [
        ast.unparse(e)
        for e in exprs
        if ast.unparse(e).split(".")[-1] in _ASSERTION_CATCHING
    ]
    return ", ".join(caught) if caught else None


def _handler_swallows(handler: ast.ExceptHandler) -> bool:
    return all(
        isinstance(stmt, (ast.Pass, ast.Continue))
        or (isinstance(stmt, ast.Return) and stmt.value is None)
        for stmt in handler.body
    )


def _own_failure_signal(body: list[ast.stmt]) -> str | None:
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assert):
                return f"assert at line {node.lineno}"
            # Cheap name test before ast.unparse: unparsing every call in
            # every try body across ~670 files is the hottest path here.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fail"
            ):
                text = ast.unparse(node.func)
                if text in ("pytest.fail", "self.fail"):
                    return f"{text}() at line {node.lineno}"
            if (
                isinstance(node, ast.Raise)
                and node.exc is not None
                and ast.unparse(node.exc).startswith("AssertionError")
            ):
                return f"raise AssertionError at line {node.lineno}"
    return None


def _self_defeating_try(node: ast.Try) -> list[tuple[int, str]]:
    """Findings for one ``try`` statement."""
    signal = _own_failure_signal(node.body)
    if signal is None:
        return []
    findings: list[tuple[int, str]] = []
    for handler in node.handlers:
        caught = _handler_catches_assertion(handler)
        if caught and _handler_swallows(handler):
            findings.append(
                (
                    handler.lineno,
                    f"`except {caught}: pass` swallows the try body's own {signal}",
                )
            )
    return findings


def find_self_defeating_try_blocks(tree: ast.AST) -> list[tuple[int, str]]:
    """Flag every try in ``tree`` that swallows its own failure signal."""
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            findings.extend(_self_defeating_try(node))
    return findings


_COLLECTED: tuple[dict[str, list[str]], dict[str, dict[str, int]]] | None = None


def _collect() -> tuple[dict[str, list[str]], dict[str, dict[str, int]]]:
    """Scan the corpus once; the three rules share the result.

    Independent per-rule walks cost a multiple of the budget. One walk per file,
    memoised across the three rule tests, is what the ``@pytest.mark.timeout``
    below is sized for.
    """
    global _COLLECTED
    if _COLLECTED is not None:
        return _COLLECTED
    findings: dict[str, list[str]] = {
        "cwd_relative_corpus_scan": [],
        "dot_git_presence_gate": [],
        "self_defeating_try": [],
    }
    coverage: dict[str, dict[str, int]] = {}
    for label, path in _iter_corpus_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        counts = coverage.setdefault(
            label,
            {
                "files": 0,
                "non_test_files": 0,
                "fs_predicates": 0,
                "corpus_calls": 0,
                "try_blocks": 0,
            },
        )
        counts["files"] += 1
        if not _is_test_module(path):
            counts["non_test_files"] += 1
        rel = _rel(path)
        # ONE walk per file. Rules 1 and 3 and every coverage counter are
        # decided from the node in hand; the tree is then dropped rather than
        # cached, which keeps ~670 ASTs out of memory at once.
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                counts["try_blocks"] += 1
                for lineno, detail in _self_defeating_try(node):
                    findings["self_defeating_try"].append(f"{rel}:{lineno}: {detail}")
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name in _FS_PRESENCE_PREDICATES:
                    counts["fs_predicates"] += 1
                elif name in _CORPUS_SCAN_CALLS:
                    counts["corpus_calls"] += 1
                    detail = _cwd_relative_scan(node)
                    if detail is not None:
                        findings["cwd_relative_corpus_scan"].append(
                            f"{rel}:{node.lineno}: {detail}"
                        )
        # Rule 2 needs scope context, so it takes a second pass -- but only over
        # the handful of files that mention `.git` at all. A file that never
        # names it cannot contain the shape, and the text pre-filter keeps the
        # scoped walk off ~98% of the corpus.
        if ".git" in source:
            for lineno, detail in find_dot_git_presence_gates(tree):
                findings["dot_git_presence_gate"].append(f"{rel}:{lineno}: {detail}")
    _COLLECTED = (findings, coverage)
    return _COLLECTED


def _assert_corpus_is_real(coverage: dict[str, dict[str, int]]) -> None:
    """Prove the scan actually reached a corpus before trusting a clean result.

    This is the whole point of the module applied to itself: a guard that
    scans zero files, or whose matcher no longer recognises any of the shapes
    it polices, passes forever.
    """
    assert coverage, (
        f"no test corpus was scanned at all. backend/testing: "
        f"{_ROOT_MISS_REASONS['backend/testing']}; scripts: "
        f"{_ROOT_MISS_REASONS['scripts']}."
    )
    for label, floors in _ROOT_FLOORS.items():
        counts = coverage.get(label)
        if counts is None:
            # A root that never got scanned is NOT a pass. Continuing here
            # would be this module's own defect class in miniature: with
            # scripts/ unresolvable the guard would report clean having walked
            # only backend/testing, and nothing would say so. Match the sibling
            # test_invariants._require_scripts_root -- hard-fail under CI,
            # where the harness is REQUIRED to make the corpus reachable, and
            # skip only on a developer layout that genuinely lacks the tree.
            message = (
                f"the {label} corpus was never scanned: "
                f"{_ROOT_MISS_REASONS.get(label, label)}. A clean result from "
                f"a scan that never reached {label} proves nothing, so this "
                f"guard refuses to report one."
            )
            if os.environ.get("CI"):
                pytest.fail(message)
            pytest.skip(message)
        for key, floor in floors.items():
            assert counts[key] >= floor, (
                f"only {counts[key]} {key} scanned under {label}; expected at "
                f"least {floor}. The guard is not reaching the corpus, so a "
                f"clean result proves nothing."
            )


def test_the_corpus_reaches_the_guard_implementations() -> None:
    """Regression: the scan must not narrow back to ``test_*.py``.

    This module shipped globbing ``test_*.py`` only. The three rules police
    guard machinery, and the machinery lives in helper modules -- so the scan
    reached the tests that CALL the guards and never the guards themselves.
    Proven by planting a rule-1, a rule-2 and a rule-3 violation in
    ``estate_inventory.py`` and ``sentinel_freshness.py``: the module still
    reported ``6 passed``.

    Asserted on the ROSTER rather than on the memoised counters so this fails
    on a narrowed glob even if ``_collect`` is never reached, and stated per
    root so losing one root cannot be covered by the other.
    """
    by_root: dict[str, list[Path]] = {}
    for label, path in _iter_corpus_files():
        by_root.setdefault(label, []).append(path)

    assert by_root, (
        f"no corpus root resolved at all. backend/testing: "
        f"{_ROOT_MISS_REASONS['backend/testing']}; scripts: "
        f"{_ROOT_MISS_REASONS['scripts']}."
    )
    for label, floor in ((k, v["non_test_files"]) for k, v in _ROOT_FLOORS.items()):
        paths = by_root.get(label)
        if paths is None:
            # Same fail-closed-under-CI / skip-on-a-partial-dev-layout split
            # that _assert_corpus_is_real uses; see its comment.
            message = (
                f"the {label} corpus root never resolved: "
                f"{_ROOT_MISS_REASONS.get(label, label)}."
            )
            if os.environ.get("CI"):
                pytest.fail(message)
            pytest.skip(message)
            continue
        non_test = sorted(p.name for p in paths if not _is_test_module(p))
        assert len(non_test) >= floor, (
            f"only {len(non_test)} non-test module(s) in the {label} corpus; "
            f"expected at least {floor}. The scan has narrowed back to "
            f"test_*.py, which leaves every guard IMPLEMENTATION under {label} "
            f"unscanned while this module goes on reporting clean. Scanned: "
            f"{non_test}"
        )


# The whole backend/testing + scripts corpus (767 files) is read and AST-walked
# once here; the two rule tests below reuse the memoised result and cost nothing.
# Measured at 32.2s on a Windows checkout (Python 3.13). That is above
# MAX_INNER_OPERATION_SECONDS and was above the then-30s lane budget, so this
# guard needs an explicit marker sized on the real cost; a marker at or near the
# measurement would kill a healthy run rather than a hung one. 300s is ~9x the
# measurement, matching the headroom test_size_offender_governance uses for its
# own corpus guard. See LANE_HANG_DETECTOR_SECONDS and
# MAX_INNER_OPERATION_SECONDS in test_harness_timeout_budgets.py.
@pytest.mark.timeout(300)
def test_no_corpus_scan_is_rooted_at_a_cwd_relative_literal() -> None:
    """Rule 1: a scan root must come from __file__, not from the CWD."""
    findings, coverage = _collect()
    _assert_corpus_is_real(coverage)
    offenders = findings["cwd_relative_corpus_scan"]
    assert not offenders, (
        f"{len(offenders)} corpus scan(s) rooted at a CWD-relative repository "
        f"path. The standalone lane runs from backend/ and the sharded lane "
        f"from /app, so at least one lane scans a tree that does not exist and "
        f"the guard passes on an empty corpus (issue #17872). Resolve the root "
        f"from Path(__file__).resolve() instead:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.timeout(300)
def test_no_guard_decides_executability_from_dot_git_presence() -> None:
    """Rule 2: presence of ``.git`` is never the predicate."""
    findings, coverage = _collect()
    _assert_corpus_is_real(coverage)
    offenders = findings["dot_git_presence_gate"]
    assert not offenders, (
        f"{len(offenders)} guard(s) decide whether they run from the PRESENCE "
        f"of .git. Both spellings are wrong: isdir under-shoots (a linked "
        f"worktree's .git is a FILE, so the guard silently skips in every "
        f"agent and dev worktree -- #17872/#17887) and exists over-shoots (a "
        f"worktree mounted into a container names a HOST gitdir that is not "
        f"there, so git dies and the guard ERRORS -- #17892). Ask git instead, "
        f"via testing.git_checkout_probe.git_readable_repo_root, and state in "
        f"the skip reason whether CI still exercises the guard:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.timeout(300)
def test_no_try_block_swallows_its_own_failure_signal() -> None:
    """Rule 3: a broad handler must not eat the test's own AssertionError."""
    findings, coverage = _collect()
    _assert_corpus_is_real(coverage)
    offenders = findings["self_defeating_try"]
    assert not offenders, (
        f"{len(offenders)} try block(s) raise their own failure signal inside "
        f"the body and then swallow it in a handler broad enough to catch "
        f"AssertionError. The test passes no matter what the code under test "
        f"does. Use pytest.raises(...) for the expected exception, and assert "
        f"on it after the block:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Mutation self-tests. A structural guard that cannot be shown to fail is
# indistinguishable from one that silently matches nothing -- which is the
# defect class this whole module is about, so it must hold itself to it.
# ---------------------------------------------------------------------------


def test_the_matchers_flag_each_known_bad_shape() -> None:
    """Each rule must actually fire on the instance that motivated it."""
    bad_scan = ast.parse(
        "import glob\n"
        "def test_x():\n"
        "    files = glob.glob('backend/app/**/*.py', recursive=True)\n"
    )
    assert find_cwd_relative_corpus_scans(bad_scan), (
        "rule 1 must flag the exact #17872 shape"
    )

    bad_git_isdir = ast.parse(
        "import os\n"
        "def _available():\n"
        "    return os.path.isdir(os.path.join(root, '.git'))\n"
    )
    assert find_dot_git_presence_gates(bad_git_isdir), (
        "rule 2 must flag the under-shooting #17887 shape"
    )

    # The overshoot must be flagged too, or the rule just walks the defect from
    # one wrong spelling to the other.
    bad_git_exists = ast.parse(
        "from pathlib import Path\n"
        "def _available():\n"
        "    return (REPO_ROOT / '.git').exists()\n"
    )
    assert find_dot_git_presence_gates(bad_git_exists), (
        "rule 2 must flag the over-shooting #17892 shape"
    )

    bad_git_inline = ast.parse(
        "import os\n"
        "import pytest\n"
        "def test_x():\n"
        "    if not os.path.exists(os.path.join(repo_root, '.git')):\n"
        "        pytest.skip('no checkout')\n"
        "    run_git()\n"
    )
    assert find_dot_git_presence_gates(bad_git_inline), (
        "rule 2 must flag an inline skip gated on .git presence"
    )

    # The shape rule 2 originally MISSED: the branch neither skips nor
    # rejects, it ANSWERS -- `return root`. This is verbatim
    # test_size_offender_governance._real_repo_checkout before #17892, i.e.
    # the instance this module's docstring is written about, so a rule that
    # cannot flag it would itself be a vacuous guard.
    bad_git_accept = ast.parse(
        "import os\n"
        "def _real_repo_checkout():\n"
        "    for root in candidates:\n"
        "        if os.path.exists(os.path.join(root, '.git')):\n"
        "            return root\n"
        "    return None\n"
    )
    assert find_dot_git_presence_gates(bad_git_accept), (
        "rule 2 must flag a root ACCEPTED on .git presence, not only a skip"
    )

    bad_try = ast.parse(
        "def test_x():\n"
        "    try:\n"
        "        do_thing()\n"
        "        assert False, 'expected rejection'\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert find_self_defeating_try_blocks(bad_try), (
        "rule 3 must flag a handler that eats the try body's own assert"
    )


def test_the_matchers_leave_the_sanctioned_shapes_alone() -> None:
    """The corrected forms must not be flagged, or the rules are unusable."""
    good_scan = ast.parse(
        "from pathlib import Path\n"
        "def test_x():\n"
        "    root = Path(__file__).resolve().parents[1] / 'app'\n"
        "    files = sorted(root.rglob('*.py'))\n"
    )
    assert not find_cwd_relative_corpus_scans(good_scan)

    # The sanctioned shape: .git is a cheap PRE-FILTER, and git itself answers
    # the question. Modelled on testing.git_checkout_probe.
    good_git = ast.parse(
        "import os, subprocess\n"
        "def _readable(root):\n"
        "    if not os.path.exists(os.path.join(root, '.git')):\n"
        "        return None\n"
        "    probe = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root)\n"
        "    return root if probe.returncode == 0 else None\n"
    )
    assert not find_dot_git_presence_gates(good_git), (
        "a .git pre-filter ahead of a real git call is the sanctioned shape"
    )

    # .github is a real directory; an isdir on it is correct and must not trip
    # the ".git" matcher.
    github_dir = ast.parse(
        "from pathlib import Path\n"
        "def _has_workflows(root):\n"
        "    return (root / '.github').is_dir()\n"
    )
    assert not find_dot_git_presence_gates(github_dir)

    good_try = ast.parse(
        "import pytest\n"
        "def test_x():\n"
        "    with pytest.raises(ValueError) as excinfo:\n"
        "        do_thing()\n"
        "    assert excinfo.value.args[0] == 'nope'\n"
    )
    assert not find_self_defeating_try_blocks(good_try)

    # A narrow handler is a legitimate control-flow sentinel, not a swallow.
    sentinel_try = ast.parse(
        "def test_x():\n"
        "    try:\n"
        "        run_loop()\n"
        "        assert calls == 3\n"
        "    except _StopLoop:\n"
        "        pass\n"
    )
    assert not find_self_defeating_try_blocks(sentinel_try)


# Three real `git` subprocesses at _GIT_STEP_TIMEOUT (20s) each is 60s of inner
# budget, against pytest-timeout's default of 60s from backend/pyproject.toml --
# an exact budget inversion with zero grace, which kills a HEALTHY run rather
# than a hung one. Match the siblings above so the outer budget exceeds the
# inner one.
@pytest.mark.timeout(300)
def test_the_three_way_truth_table_behind_rule_2_actually_holds(tmp_path) -> None:
    """Independent proof of rule 2's premise, not a restatement of the rule.

    Rule 2 rests on a claim about git that nothing else in the repo
    demonstrates. So build the three real layouts and measure all three
    predicates against each:

    ==================  ======  =========  ==================
    layout              isdir   exists     git rev-parse HEAD
    ==================  ======  =========  ==================
    real clone          True    True       succeeds
    worktree pointer    False   True       fails
    no checkout         False   False      fails
    ==================  ======  =========  ==================

    The middle row is the whole defect class: ``isdir`` reads it as "no
    checkout" and skips a guard that could have run, while ``exists`` reads it
    as "checkout" and hands git a path it cannot open -- which is an ERROR, not
    a skip. Only the third column separates the cases correctly.
    """
    if shutil.which("git") is None:
        pytest.skip(
            "no git binary in this environment, so the probe column of the "
            "truth table cannot be measured. CI coverage: the standalone lane's "
            "image carries git (test_size_offender_governance runs git there), "
            "so this executes rather than skipping in CI."
        )

    clone = tmp_path / "clone"
    clone.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    # 20s, not 30s: 30 is MAX_INNER_OPERATION_SECONDS in
    # testing/test_harness_timeout_budgets.py, the ceiling at or above which an
    # inner timeout must declare its own marker -- the harness-budget-inversion
    # class that module exists to catch (#17674, #17681). Sized against that
    # ceiling, not against the lane's LANE_HANG_DETECTOR_SECONDS.
    # These calls touch a two-file tmp repo and finish in milliseconds.
    _GIT_STEP_TIMEOUT = 20

    def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", f"safe.directory={cwd}", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_STEP_TIMEOUT,
            check=False,
        )

    assert _git("init", "-q", cwd=clone).returncode == 0
    (clone / "f.txt").write_text("x", encoding="utf-8")
    assert _git("add", "f.txt", cwd=clone).returncode == 0
    commit = subprocess.run(
        ["git", "-c", f"safe.directory={clone}", "commit", "-qm", "seed"],
        cwd=str(clone),
        capture_output=True,
        text=True,
        timeout=_GIT_STEP_TIMEOUT,
        env=env,
        check=False,
    )
    assert commit.returncode == 0, commit.stderr

    # Row 1: a real clone.
    clone_git = clone / ".git"
    assert clone_git.is_dir() and clone_git.exists()
    assert git_readable_repo_root(clone) == str(clone), (
        "the probe must ACCEPT a real clone -- a rule that skipped everywhere "
        "would be the very defect this file polices"
    )

    # Row 2: a linked worktree whose gitdir is not reachable from here. This is
    # exactly what a worktree checkout looks like from inside a container: the
    # pointer names a HOST path.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {tmp_path / 'nowhere' / '.git' / 'worktrees' / 'wt'}\n",
        encoding="utf-8",
    )
    wt_git = worktree / ".git"
    assert not wt_git.is_dir(), (
        "a linked worktree's .git is a FILE; isdir therefore reports 'no "
        "checkout' and silently skips the guard -- the #17872/#17887 defect"
    )
    assert wt_git.exists(), (
        "exists() accepts the pointer, which is why it OVERSHOOTS: git is then "
        "handed an unreachable gitdir and the guard errors -- the #17892 defect"
    )
    assert _git("rev-parse", "HEAD", cwd=worktree).returncode != 0, (
        "git must fail on an unreachable gitdir; if it ever succeeds, the "
        "probe would wrongly enable a guard that cannot read the repo"
    )
    assert git_readable_repo_root(worktree) is None, (
        "the probe must decline an unreachable worktree pointer -- skipping, "
        "not erroring"
    )

    # Row 3: no checkout at all.
    bare = tmp_path / "bare"
    bare.mkdir()
    assert not (bare / ".git").exists()
    assert git_readable_repo_root(bare) is None


if __name__ == "__main__":  # pragma: no cover - developer convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
