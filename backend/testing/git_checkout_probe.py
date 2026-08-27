"""Decide whether git can actually READ a repo root, from where a test is running.

Guards that shell out to git need a precondition: is there a usable checkout
here at all? The obvious spelling -- look for ``.git`` -- is wrong in both
directions, and this repo has now shipped both wrong spellings:

===================================  =================  ============================  ==========
check                                worktree on host   worktree mounted in container  real clone
===================================  =================  ============================  ==========
``os.path.isdir(root/".git")``       skips (WRONG)      skips (WRONG)                  runs
``os.path.exists(root/".git")``      runs               **errors** (WORSE)             runs
``git rev-parse HEAD``               runs               skips, with a reason           runs
===================================  =================  ============================  ==========

``isdir`` is wrong because a LINKED WORKTREE's ``.git`` is a FILE holding
``gitdir: <path>``, so the guard silently skipped in every agent worktree and
every dev worktree and only ever ran in a real clone (#17872 / #17887).
``exists`` fixes that and overshoots: a worktree's ``gitdir:`` line names a
HOST path that does not exist inside a container, so git dies with
``fatal: not a git repository: /repo/D:/.../.git/worktrees/<name>`` and the
guard ERRORS instead of skipping (#17892).

**Presence of .git is never the predicate.** Ask git the cheapest question that
fails in exactly the cases the guard's real git call would -- unreachable
gitdir, unreadable object store, unresolvable HEAD.
"""

from __future__ import annotations

import os
import subprocess

# Comfortably under MAX_INNER_OPERATION_SECONDS (backend/testing/
# test_harness_timeout_budgets.py), the ceiling at which an inner call must
# declare its own @pytest.mark.timeout: this reads one ref. A budget that cannot
# fire before the harness kills the test is the defect class that module guards.
# The CEILING binds here, not the lane's far larger LANE_HANG_DETECTOR_SECONDS.
_PROBE_TIMEOUT_SECONDS = 10


def git_readable_repo_root(*candidates: str | os.PathLike[str]) -> str | None:
    """Return the first candidate git can read, or None if git can read none.

    Pass exactly the roots the caller intends to run git against, in preference
    order. Nothing is appended: a probe that succeeds on a root the caller then
    does not use would re-create the overshoot it exists to prevent.

    The CI backend shards bind the checkout read-only into a root container
    while the runner user owns the files, so git refuses every command with
    "detected dubious ownership" (exit 128) unless the path is marked safe.
    ``-c safe.directory=<root>`` is the same injection runner_isolation.py
    performs via ``GIT_CONFIG_*`` for cloned workspaces. Callers running their
    own git commands against the returned root must pass it too.
    """
    for candidate in candidates:
        root = str(candidate)
        if not os.path.exists(os.path.join(root, ".git")):
            # Cheap pre-filter only, NOT the predicate: a ``.git`` that is
            # present can still be unreadable, which is what the probe below
            # decides.
            continue
        try:
            probe = subprocess.run(
                ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # No git binary, or the probe could not be spawned. Either way git
            # cannot serve this root; try the next one.
            continue
        if probe.returncode == 0 and probe.stdout.strip():
            return root
    return None


def unreadable_repo_skip_reason(ci_coverage: str) -> str:
    """Build the skip reason for a guard the git probe could not enable.

    ``ci_coverage`` must state, plainly, whether CI still exercises this guard.
    A silent skip -- or one that names no case -- is what kept this defect class
    invisible: nobody could tell a skip that is expected from a skip that has
    quietly disabled an invariant everywhere.
    """
    return (
        "git cannot read a checkout of this repo at the root this guard uses, "
        "so it has nothing to compare against. Two cases produce it: (1) the "
        "backend-only Docker context, whose repo root is `/` and carries no "
        "checkout; (2) a LINKED WORKTREE mounted into a container -- its .git "
        "file names a HOST gitdir that does not exist inside the container. "
        f"CI coverage: {ci_coverage}"
    )
