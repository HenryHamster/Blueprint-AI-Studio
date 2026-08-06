"""The repo-root ``./check`` gate must fail closed when the base diff is uncomputable.

Regression target: the changed-file computation in ``./check`` used to end in
``git diff ... 2>/dev/null || true``. When the base ref could not be resolved —
the NORMAL case on a truncated/shallow clone in CI and on task VMs — the error
was swallowed, the changed-set came back empty, every downstream gate found
nothing to check, and the run printed ``PASS`` with zero checks actually
executed. The empty set is indistinguishable in that code path from a genuine
no-op diff, so the gate reported green exactly when it was least entitled to.

This module drives the real ``./check`` script against throwaway git repos:

  * ``test_uncomputable_base_diff_fails_closed`` — a repo with no base ref and
    no remote must exit non-zero with a diagnostic naming the base ref it tried
    to resolve, never a green run.
  * ``test_genuine_noop_diff_still_exits_zero`` — a repo whose base ref
    resolves and whose committed diff is genuinely empty must still exit 0, so
    the fail-closed change does not break legitimate empty diffs.

The tests invoke the script as ``bash <repo>/check`` with ``WORKSPACE`` pointed
at the throwaway repo, so they exercise the exact production code path (the
``_try_base_diff`` / fetch-repair / fail-closed block), not a reimplementation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if not (_REPO_ROOT / "check").exists() and Path("/repo/check").exists():
    _REPO_ROOT = Path("/repo")
_CHECK = _REPO_ROOT / "check"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the throwaway repo."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    """Bootstrap a minimal git repo with one committed file."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "check-gate-test@example.com")
    _git(repo, "config", "user.name", "check gate test")
    # A host-level gpg signing config would otherwise make the throwaway commit
    # try to sign (and fail) for reasons unrelated to the gate under test.
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "initial")


def _run_check(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the tracked ``./check`` script against the throwaway repo."""
    env = os.environ.copy()
    env["WORKSPACE"] = str(repo)
    return subprocess.run(
        ["bash", str(_CHECK), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_uncomputable_base_diff_fails_closed(tmp_path: Path) -> None:
    """An unresolvable base ref is a hard failure, never a green run.

    The repo has a commit, but no ``origin/automain``, no local ``automain``,
    and no ``origin`` remote to fetch one from — so ``git diff ...automain...HEAD``
    cannot be computed. The gate must exit non-zero and name the base ref it
    tried to resolve, with an actionable fetch instruction.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash:
        pytest.skip("bash not available on this host")
    if not git:
        pytest.skip("git not available on this host")

    repo = tmp_path / "repo"
    _init_repo(repo)
    # No origin remote and no base ref: the committed diff is uncomputable.
    result = _run_check(repo)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        "check must FAIL CLOSED when the base diff cannot be computed; "
        f"got exit 0 with output:\n{combined}"
    )
    assert "automain" in combined, (
        "the diagnostic must name the base ref it tried to resolve; "
        f"output:\n{combined}"
    )
    assert "fetch" in combined.lower(), (
        "the diagnostic must tell the caller what to fetch; "
        f"output:\n{combined}"
    )


def test_genuine_noop_diff_still_exits_zero(tmp_path: Path) -> None:
    """A computed-but-empty committed diff must keep exiting zero.

    The repo has a local ``automain`` branch at HEAD and a clean working tree,
    so ``git diff automain...HEAD`` succeeds and genuinely reports no changed
    files. This is the legitimate pass the fail-closed change must preserve.
    """
    bash = shutil.which("bash")
    git = shutil.which("git")
    if not bash:
        pytest.skip("bash not available on this host")
    if not git:
        pytest.skip("git not available on this host")

    repo = tmp_path / "repo"
    _init_repo(repo)
    # Local base ref at HEAD -> the committed diff computes and is empty.
    _git(repo, "branch", "automain")

    result = _run_check(repo)

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, (
        "a genuine no-op diff must still pass; "
        f"exit {result.returncode} with output:\n{combined}"
    )
    assert "PASS" in combined, f"expected the green PASS marker, got:\n{combined}"
