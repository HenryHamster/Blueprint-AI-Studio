"""A guard that shells out to git must be able to LOOK before it reports.

THE DEFECT CLASS
----------------
Three unrelated conditions all end with git refusing to answer, and all three
are silent at the call site because the caller reads a non-zero exit as "the
thing I asked about is not there":

* **OWNERSHIP.** ``fatal: detected dubious ownership in repository at ...``,
  exit 128, for EVERY subcommand. This is the common one in CI: the backend
  shards mount the runner-user's checkout into a container that runs as root
  (``docker run -v "$PWD:/repo:ro"``), and the runner is installed on operator
  hosts as a systemd unit whose ``User=`` is not the uid that owns the
  checkout. The repair is ``-c safe.directory=<root>`` on the invocation.
* **SHALLOWNESS.** ``actions/checkout@v4`` clones at depth 1, so ``origin/<ref>``
  and any history behind the tip simply are not present.
* **LAYOUT.** Inside a linked worktree ``.git`` is a FILE holding
  ``gitdir: <path>``, so an ``os.path.isdir(".git")`` probe concludes "not a
  repository" about a repository.

WHY A STRUCTURAL SWEEP AND NOT JUST PER-SITE TESTS
--------------------------------------------------
The live instance was an ASYMMETRY, not an omission: one code path's *probe*
had been hardened with ``-c safe.directory=`` while the actual *read* beside it
had not, so the guard proved it could look and then looked with a command that
could not. Per-site tests do not catch a new site, and the failure mode is a
guard that reports CLEAN. The sweep below is the thing that cannot be
regrown one call site at a time.

WHAT THIS FILE DELIBERATELY DOES NOT DO
---------------------------------------
Several of the swept modules FAIL CLOSED when git is unreadable -- "Refusing
cap bump -- decomposition required until history is readable", "<ws> is not a
git checkout", ``SystemExit("not inside a git repository")``. That stance is
correct and nothing here softens it. The rule is about making the READ work,
never about tolerating a read that failed.

Safe to run standalone: no DB, no network, no Docker. Every git invocation is
against a repository this file creates in ``tmp_path``.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
# scripts/*.py tests run with --noconftest, so the shared offender-identity
# producer is reached by the same explicit path insertion the other scripts
# guards use rather than by a conftest-installed sys.path entry.
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

import verify  # noqa: E402
from testing.fixgate_attribution import format_offender_marker  # noqa: E402

# ---------------------------------------------------------------------------
# The structural sweep
# ---------------------------------------------------------------------------

# Non-test modules that run over a checkout they did not create. Test modules
# are excluded on purpose: a test builds its OWN fixture repository in
# ``tmp_path``, owned by the process under test, so there is no foreign owner
# to name -- and the tests that DO stage the condition have to be able to issue
# an unhardened control command to prove the condition is staged at all.
_CORPUS_ROOTS = (
    ("scripts", "*.py"),
    ("backend/app", "*.py"),
    ("backend/testing", "*.py"),
    ("agent/src/_agent_scripts", "*.py"),
)

# A textual pre-filter for the AST pass: the rule can only fire on a list whose
# first element is the literal ``git``, so a file containing neither spelling
# cannot contain a match. It is a speed optimisation over ~600 files (4.8s of
# parsing becomes 1.2s), and _FLOOR below is what keeps it from becoming a way
# for the sweep to silently inspect nothing.
_GIT_LITERALS = ('"git"', "'git'")

# Modules known to issue git argv today. If the sweep stops seeing these, it is
# not clean -- it is blind: a missing bind mount, a piecemeal fixture checkout,
# or a pre-filter that stopped matching. Assert the floor, never the exact set,
# so adding a module is not a chore and removing one is not silent.
_FLOOR = (
    "scripts/blueprint.py",
    "scripts/blueprint_runner.py",
    "scripts/derived_artifact_merge_resolve.py",
    "scripts/local_runner.py",
    "scripts/verify.py",
    "scripts/walkthrough_stack.py",
    "backend/app/size_limit_policy.py",
    "backend/testing/check_size_limits.py",
)

# ``git clone`` and ``git init`` CREATE the repository, so there is no existing
# repository whose ownership could be named safe -- ``safe.directory`` on them
# is meaningless rather than merely redundant.
_REPOSITORY_CREATING = frozenset({"clone", "init"})


def _string_constant(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _git_argv_elements(node: ast.AST) -> list[ast.expr] | None:
    """The elements of ``node``'s first argument when it is a git argv list."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    first = node.args[0]
    if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
        return None
    if _string_constant(first.elts[0]) != "git":
        return None
    return list(first.elts)


def _subcommand_words(elements: list[ast.expr]) -> set[str]:
    return {
        value for value in (_string_constant(e) for e in elements) if value is not None
    }


def _names_a_safe_directory(elements: list[ast.expr]) -> bool:
    """True when the argv carries a ``-c safe.directory=...`` pair.

    The value is almost always an f-string over a path variable, so accept any
    interpolated value after ``-c`` as well as a literal ``safe.directory=``.
    """
    for index, element in enumerate(elements):
        if _string_constant(element) != "-c" or index + 1 >= len(elements):
            continue
        value = elements[index + 1]
        if isinstance(value, ast.JoinedStr):
            return True
        literal = _string_constant(value)
        if literal is not None and literal.startswith("safe.directory="):
            return True
    return False


def _creates_its_own_repository(scope: ast.AST) -> bool:
    """True when *scope* clones or inits the repository it goes on to read.

    ``compute_backends._git_clone_ref`` is the shape: it clones into ``dest``
    and then runs ``git -C dest checkout <ref>`` on the tree it just made, as
    the same uid, in the same process. There is no foreign owner to name.
    """
    for node in ast.walk(scope):
        elements = _git_argv_elements(node)
        if elements and _subcommand_words(elements) & _REPOSITORY_CREATING:
            return True
    return False


def _unhardened_git_reads(source: str) -> list[int]:
    """Line numbers of git invocations in *source* that cannot name a root."""
    tree = ast.parse(source)
    exempt_lines: set[int] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _creates_its_own_repository(scope):
            continue
        for node in ast.walk(scope):
            elements = _git_argv_elements(node)
            if elements is not None:
                exempt_lines.add(node.lineno)

    offenders: list[int] = []
    for node in ast.walk(tree):
        elements = _git_argv_elements(node)
        if elements is None or node.lineno in exempt_lines:
            continue
        if _subcommand_words(elements) & _REPOSITORY_CREATING:
            continue
        if _names_a_safe_directory(elements):
            continue
        offenders.append(node.lineno)
    return offenders


def _corpus() -> list[Path]:
    paths: list[Path] = []
    for relative, pattern in _CORPUS_ROOTS:
        directory = REPOSITORY_ROOT / relative
        if not directory.is_dir():
            continue
        paths.extend(
            path
            for path in sorted(directory.glob(pattern))
            if not path.name.startswith("test_")
        )
    return paths


def _sweep() -> tuple[list[str], set[str]]:
    """Return (offenders, modules actually parsed) over the whole corpus."""
    offenders: list[str] = []
    parsed: set[str] = set()
    for path in _corpus():
        source = path.read_text(encoding="utf-8")
        if not any(literal in source for literal in _GIT_LITERALS):
            continue
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        parsed.add(relative)
        offenders.extend(f"{relative}:{line}" for line in _unhardened_git_reads(source))
    return offenders, parsed


def test_every_git_read_names_the_checkout_it_reads_as_safe() -> None:
    offenders, parsed = _sweep()
    missing_floor = [relative for relative in _FLOOR if relative not in parsed]
    assert not missing_floor, (
        "the sweep did not inspect modules that are known to shell out to git, "
        "so a clean result here would be blindness rather than health: "
        f"{missing_floor}"
        + "\n"
        + format_offender_marker(missing_floor)
    )
    assert not offenders, (
        "git refuses EVERY subcommand with 'detected dubious ownership' (exit "
        "128) when the calling uid does not own the checkout -- the shape a "
        "container running as root or a systemd unit with a foreign User= hits "
        "routinely. Pass -c safe.directory=<root> on the invocation so the read "
        "WORKS; do not soften what a genuine failure means: "
        + repr(offenders)
        + "\n"
        + format_offender_marker(offenders)
    )


def test_the_sweep_can_actually_fail(tmp_path: Path) -> None:
    """A guard nobody has watched fail is a guard nobody has tested.

    Both halves matter: an unhardened read must be reported, and the hardened
    form of the same read must not be.
    """
    unhardened = 'subprocess.run(["git", "rev-parse", "HEAD"], cwd=root)\n'
    hardened = (
        'subprocess.run(\n'
        '    ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"], cwd=root\n'
        ')\n'
    )
    assert _unhardened_git_reads(unhardened) == [1]
    assert _unhardened_git_reads(hardened) == []


def test_repository_creating_commands_are_exempt_structurally() -> None:
    """``clone``/``init`` have no repository yet whose owner could be named.

    The exemption is derived from the code, not from a path allowlist: a
    function that creates the repository it then reads covers the whole
    clone-then-checkout shape without anyone maintaining a list.
    """
    assert _unhardened_git_reads('subprocess.run(["git", "clone", url, dest])\n') == []
    clone_then_read = (
        "def fetch(url, dest):\n"
        '    subprocess.run(["git", "clone", url, dest])\n'
        '    subprocess.run(["git", "-C", dest, "checkout", ref])\n'
    )
    assert _unhardened_git_reads(clone_then_read) == []
    # But a read in a function that creates nothing is still reported.
    plain_read = (
        "def look(dest):\n"
        '    subprocess.run(["git", "-C", dest, "checkout", ref])\n'
    )
    assert _unhardened_git_reads(plain_read) == [2]


# ---------------------------------------------------------------------------
# scripts/verify.py, end to end over a real exit-128 git
# ---------------------------------------------------------------------------
#
# ``_get_changed_files`` is the FAIL-OPEN member of the family and so the worst
# shape in it: both reads ignore their return code, so a git that exited 128
# yields an EMPTY changed-file set and every ``--changed`` check then prints
# "No changed files" and returns True. The pass reports green having inspected
# nothing at all.

_DUBIOUS_OWNERSHIP_RC = 128


def _init_repo(root: Path) -> None:
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(argv, cwd=root, capture_output=True, timeout=15, check=False)


def _foreign_owned_git_or_skip(root: Path) -> None:
    """Stage "another user owns this checkout" without needing a second uid.

    ``GIT_TEST_ASSUME_DIFFERENT_OWNER=1`` is git's own switch for exactly this
    condition, which is what lets a single-uid container reproduce the real CI
    failure. Skip rather than assert when this git cannot stage it, so the pin
    never becomes a platform-specific red.
    """
    env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    kw = {"cwd": root, "capture_output": True, "text": True, "env": env,
          "timeout": 15}
    bare = subprocess.run(["git", "rev-parse", "--show-toplevel"], **kw)
    safe = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "rev-parse", "--show-toplevel"], **kw
    )
    if bare.returncode != _DUBIOUS_OWNERSHIP_RC or safe.returncode != 0:
        pytest.skip("this git cannot stage the dubious-ownership condition here")
    assert "dubious ownership" in bare.stderr, bare.stderr


def test_verify_sees_changed_files_in_a_foreign_owned_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("x = 1" + chr(10), encoding="utf-8")
    _init_repo(repo)
    (repo / "app" / "main.py").write_text("x = 2" + chr(10), encoding="utf-8")
    _foreign_owned_git_or_skip(repo)

    monkeypatch.setattr(verify, "PROJECT_ROOT", str(repo))
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    changed = verify._get_changed_files()

    # Unhardened this is [], and `verify.py --changed` then reports PASS for a
    # file it never compiled. Note ``git diff`` does not even fail
    # recognisably under this condition: refused the repository, it falls
    # through to ``--no-index`` mode, prints a usage message on stderr and
    # leaves stdout EMPTY -- which is exactly what "nothing changed" looks like
    # to a caller that ignores the return code.
    assert "app/main.py" in changed, changed
