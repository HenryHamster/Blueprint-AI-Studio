"""Fail-closed measurement of operator-authored commits in a release range."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


BOT_COMMIT_AUTHOR = "blueprintagentorchestrator[bot]"
MAX_REPORTED_OFFENDERS = 10
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _unresolved(
    base_sha: str | None,
    head_sha: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "state": "unresolved",
        "count": None,
        "offenders": [],
        "range": {"base_sha": base_sha, "head_sha": head_sha},
        "reason": reason,
    }


def _git(
    args: list[str],
    *,
    repo_root: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo_root}", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def count_non_bot_authored_commits(
    base_sha: str | None,
    head_sha: str | None,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Count non-bot authors in ``base_sha..head_sha``.

    The measurement is deliberately fail-closed. Missing refs, a non-ancestor
    base, an unavailable Git executable, and shallow/truncated history all
    return ``state=unresolved`` with ``count=None``. A missing history must
    never become the false zero that would make the release success bar pass.
    """
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    if not (
        isinstance(base_sha, str)
        and isinstance(head_sha, str)
        and _FULL_SHA.fullmatch(base_sha)
        and _FULL_SHA.fullmatch(head_sha)
    ):
        return _unresolved(base_sha, head_sha, "missing_or_malformed_sha")

    try:
        shallow = _git(["rev-parse", "--is-shallow-repository"], repo_root=root)
        if shallow.returncode != 0:
            return _unresolved(base_sha, head_sha, "git_history_unavailable")
        if shallow.stdout.strip().lower() == "true":
            return _unresolved(base_sha, head_sha, "shallow_repository")

        for sha in (base_sha, head_sha):
            resolved = _git(["rev-parse", "--verify", f"{sha}^{{commit}}"], repo_root=root)
            if resolved.returncode != 0 or not _FULL_SHA.fullmatch(resolved.stdout.strip()):
                return _unresolved(base_sha, head_sha, "commit_not_resolved")

        ancestry = _git(
            ["merge-base", "--is-ancestor", base_sha, head_sha], repo_root=root
        )
        if ancestry.returncode != 0:
            return _unresolved(base_sha, head_sha, "range_not_ancestor")

        history = _git(
            ["log", f"{base_sha}..{head_sha}", "--format=%H%x00%an"],
            repo_root=root,
        )
        if history.returncode != 0:
            return _unresolved(base_sha, head_sha, "git_history_unavailable")
    except (OSError, subprocess.SubprocessError, ValueError):
        return _unresolved(base_sha, head_sha, "git_history_unavailable")

    offenders: list[dict[str, str]] = []
    for record in history.stdout.splitlines():
        sha, separator, author = record.partition("\x00")
        if not separator or not _FULL_SHA.fullmatch(sha):
            return _unresolved(base_sha, head_sha, "malformed_git_history")
        if author.casefold() != BOT_COMMIT_AUTHOR.casefold():
            if len(offenders) < MAX_REPORTED_OFFENDERS:
                offenders.append({"sha": sha[:12], "author": author})

    total = sum(
        1
        for record in history.stdout.splitlines()
        if record.partition("\x00")[1]
        and record.partition("\x00")[2].casefold() != BOT_COMMIT_AUTHOR.casefold()
    )
    return {
        "state": "resolved",
        "count": total,
        "offenders": offenders,
        "range": {"base_sha": base_sha, "head_sha": head_sha},
    }
