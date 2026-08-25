"""Resolve the repo checkout a guard can actually READ, in every lane this suite runs in.

A repo-scanning guard has to clear two independent traps before it may claim it
enforced anything, and this repo has shipped a guard that fell into each.

**1. WHERE the repo root is.**  Every containerised lane bind-mounts *backend/*
at ``/app`` and the repo root separately at ``/repo``::

    docker run -v "$PWD/backend:/app:ro" -v "$PWD:/repo:ro" ...   # ci.yml shard
    DOCKER_ARGS+=(-v "${REPO_HOST_PATH}:/repo:ro")                # scripts/test-backend.sh

so ``Path(__file__).parents[2]`` -- the spelling that is correct in a plain
checkout -- resolves to ``/`` inside every one of them.  A guard that looks only
there finds no checkout, and if it then calls ``pytest.skip`` it is green-by-skip
on *every* CI run: it has never once enforced anything in the gate it exists to
protect.  That is the same silent-pass failure mode as a vacuous guard, and it is
why nothing in this module skips because a path failed to resolve.

**2. WHETHER git can read it.**  The presence of ``.git`` is never the predicate::

    layout                              .git isdir   .git exists   git rev-parse HEAD
    real clone                          True         True          succeeds
    linked worktree, on the host        False        True          succeeds
    linked worktree, in a container     False        True          FAILS (host gitdir)
    no checkout (``/``)                 False        False         fails

``isdir`` skips in every agent and dev worktree (#17872 / #17887); ``exists``
overshoots and makes the guard ERROR inside a container, because a worktree's
``gitdir:`` line names a HOST path (#17892).  Only asking git separates the
cases, so :func:`git_readable_repo_root` asks git.

Callers get two levels of strictness:

* :func:`require_repo_root` -- for reading tracked FILES out of the checkout.
  Fails loudly when no candidate holds this repo.  Never skips.
* :func:`require_git_readable_repo_root` -- for guards that must shell out to
  git.  Fails loudly whenever a real clone is in view, which is always the case
  in CI because ``actions/checkout`` produces one.  The single case it skips on
  is the container-mounted linked worktree, where the unreachable host
  ``gitdir:`` is a property of the developer's local layout and no git
  assertion is possible; that skip names the case and is unreachable in CI.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# ``backend/testing/repo_checkout.py`` -> ``backend/``.  Correct in every lane:
# the backend tree is the thing that gets mounted, so this anchor never moves.
BACKEND_ROOT = Path(__file__).resolve().parent.parent

# The repo-root bind mount used by ci.yml's backend shards and by
# scripts/test-backend.sh.  Listed FIRST: inside those containers it is the only
# path that holds the checkout.
CONTAINER_REPO_MOUNT = Path("/repo")

# Ordered candidates for the repo root.  The plain-checkout spelling stays as
# the fallback for bare local pytest and the bwrap/sandbox lane, which run
# against the real tree with no mounts at all.
REPO_ROOT_CANDIDATES: tuple[Path, ...] = (
    CONTAINER_REPO_MOUNT,
    BACKEND_ROOT.parent,
)

# Entries that exist at the root of THIS repo and nowhere near the filesystem
# root, so ``/`` can never be mistaken for a checkout.  Deliberately not
# ``.git``: see the module docstring's truth table.
_REPO_ROOT_MARKERS: tuple[str, ...] = ("backend/app", "scripts", ".gitignore")

# Two orders of magnitude under the lanes' ``--timeout=30``: these read one ref
# or one index.  An inner budget that cannot fire before the harness kills the
# test is the budget-inversion class
# backend/testing/test_harness_timeout_budgets.py polices.
GIT_TIMEOUT_SECONDS = 10


def _missing_markers(root: Path) -> list[str]:
    """Markers from :data:`_REPO_ROOT_MARKERS` that ``root`` does not carry."""
    missing = []
    for marker in _REPO_ROOT_MARKERS:
        try:
            present = (root / marker).exists()
        except OSError:
            present = False
        if not present:
            missing.append(marker)
    return missing


def _holds_this_repo(root: Path) -> bool:
    """True when ``root`` carries this repo's top-level layout."""
    return not _missing_markers(root)


def find_repo_root() -> Path | None:
    """First candidate that holds this repo, or ``None`` when none does."""
    for candidate in REPO_ROOT_CANDIDATES:
        if _holds_this_repo(candidate):
            return candidate
    return None


def repo_root_diagnosis() -> str:
    """Per-candidate rendering of what was looked for and what was found."""
    parts = []
    for candidate in REPO_ROOT_CANDIDATES:
        missing = _missing_markers(candidate)
        parts.append(
            f"{candidate}: "
            + ("holds this repo" if not missing else f"missing {', '.join(missing)}")
        )
    return "; ".join(parts)


def git_command(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command against ``root``.

    ``-c safe.directory=<root>``: the CI shards bind the checkout read-only into
    a container running as root while the runner user owns the files, so git
    otherwise refuses every command with "detected dubious ownership" (exit
    128).  Same injection scripts/runner_isolation.py performs via
    ``GIT_CONFIG_*`` for cloned workspaces.
    """
    return subprocess.run(
        ["git", "-c", f"safe.directory={root}", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


def require_repo_root() -> Path:
    """Repo root, or a loud failure.  NEVER skips.

    A guard that cannot see the tree it polices has verified nothing, so
    reporting clean is the one outcome that must be impossible here.
    """
    root = find_repo_root()
    if root is not None:
        return root
    pytest.fail(
        "No repo checkout is visible to this test, so the guard below cannot "
        "look at the thing it polices and must not report clean.\n"
        f"  candidates: {repo_root_diagnosis()}\n"
        "  Containerised lanes must bind-mount the repo root at /repo; "
        "ci.yml's backend shards and scripts/test-backend.sh both do. "
        "backend/ is mounted at /app, so walking up from this file lands on "
        "'/', which carries no checkout."
    )


def git_readable_repo_root() -> Path | None:
    """First repo-root candidate git can actually read, else ``None``."""
    for candidate in REPO_ROOT_CANDIDATES:
        if not _holds_this_repo(candidate):
            continue
        try:
            probe = git_command(candidate, "rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError):
            # No git binary, or the probe could not be spawned.  git cannot
            # serve this root either way; try the next candidate.
            continue
        if probe.returncode == 0 and probe.stdout.strip():
            return candidate
    return None


def require_git_readable_repo_root() -> Path:
    """Repo root git can read, or a loud failure.

    Skips in exactly one case, named explicitly: a linked worktree mounted into
    a container, whose ``.git`` file points at a host ``gitdir:`` the container
    cannot reach.  That is a property of the developer's local layout, not of
    the repo, and no git assertion is possible there.  It cannot occur in CI --
    ``actions/checkout`` produces a real clone whose ``.git`` is a directory --
    so this function can never turn a CI run green by skipping.
    """
    root = git_readable_repo_root()
    if root is not None:
        return root

    visible = find_repo_root()
    worktree_pointer = visible is not None and (visible / ".git").is_file()
    if worktree_pointer and not os.environ.get("CI"):
        pytest.skip(
            f"{visible}/.git is a linked-worktree pointer whose gitdir is not "
            "reachable from here (a worktree bind-mounted into a container "
            "names a HOST path), so git cannot read this checkout. Local "
            "layout only: CI checks out a real clone, where this branch is "
            "unreachable and the guard runs."
        )
    pytest.fail(
        "git cannot read a checkout of this repo, so the guard below cannot "
        "look at the thing it polices and must not report clean.\n"
        f"  candidates: {repo_root_diagnosis()}\n"
        f"  repo root found: {visible}\n"
        "  Containerised lanes must bind-mount the repo root at /repo, and git "
        "must be installed in the image."
    )
