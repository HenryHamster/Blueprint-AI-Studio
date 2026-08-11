"""Fail-closed measurement of operator-authored commits in a release range.

The deployed backend image intentionally has no checkout or ``git`` binary.
Production callers therefore resolve release history through the authenticated
GitHub API; an explicit ``repo_root`` is retained only for local fixtures.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any


BOT_COMMIT_AUTHOR = "blueprintagentorchestrator[bot]"
MAX_REPORTED_OFFENDERS = 10
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_COMMITS_PER_PAGE = 100


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


def _count_non_bot_authored_commits_from_git(
    base_sha: str,
    head_sha: str,
    *,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Count non-bot authors from an explicit local fixture checkout."""
    root = Path(repo_root)
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
    records = history.stdout.splitlines()
    for record in records:
        sha, separator, author = record.partition("\x00")
        if not separator or not _FULL_SHA.fullmatch(sha):
            return _unresolved(base_sha, head_sha, "malformed_git_history")
        if (
            author.casefold() != BOT_COMMIT_AUTHOR.casefold()
            and len(offenders) < MAX_REPORTED_OFFENDERS
        ):
            offenders.append({"sha": sha[:12], "author": author})

    return {
        "state": "resolved",
        "count": sum(
            1
            for record in records
            if record.partition("\x00")[1]
            and record.partition("\x00")[2].casefold() != BOT_COMMIT_AUTHOR.casefold()
        ),
        "offenders": offenders,
        "range": {"base_sha": base_sha, "head_sha": head_sha},
    }


async def _count_non_bot_authored_commits_from_github(
    base_sha: str,
    head_sha: str,
    *,
    session: Any,
    project: Any,
) -> dict[str, Any]:
    """Resolve the exact immutable release range through GitHub's API.

    ``compare`` proves both ancestry and the expected number of commits.  The
    paginated commits endpoint then supplies authorship evidence; seeing the
    base SHA at exactly ``ahead_by`` entries prevents a moving ref, truncated
    page, or different history walk from being mistaken for this release range.
    """
    from app import github
    from app.release_promotion import _resolve_github_with_session

    try:
        resolved = await _resolve_github_with_session(session, project)
    except Exception:  # noqa: BLE001 - credentials are fail-closed evidence
        return _unresolved(base_sha, head_sha, "github_credentials_unavailable")
    if resolved is None:
        return _unresolved(base_sha, head_sha, "github_credentials_unavailable")
    token, owner, repo = resolved
    project_id = str(project.id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        comparison = await github._github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
            headers,
            project_id=project_id,
            consumer="release_commit_authorship",
        )
        if comparison.status_code != 200:
            return _unresolved(base_sha, head_sha, "github_range_unavailable")
        comparison_data = comparison.json()
        status = comparison_data.get("status")
        ahead_by = comparison_data.get("ahead_by")
        if status not in {"identical", "ahead"}:
            return _unresolved(base_sha, head_sha, "range_not_ancestor")
        if isinstance(ahead_by, bool) or not isinstance(ahead_by, int) or ahead_by < 0:
            return _unresolved(base_sha, head_sha, "malformed_github_range")
        if ahead_by == 0:
            return {
                "state": "resolved",
                "count": 0,
                "offenders": [],
                "range": {"base_sha": base_sha, "head_sha": head_sha},
            }

        records: list[dict[str, str]] = []
        page_number = 1
        while True:
            response = await github._github_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/commits",
                headers,
                params={"sha": head_sha, "per_page": _COMMITS_PER_PAGE, "page": page_number},
                project_id=project_id,
                consumer="release_commit_authorship",
            )
            if response.status_code != 200:
                return _unresolved(base_sha, head_sha, "github_history_unavailable")
            page = response.json()
            if not isinstance(page, list) or not page:
                return _unresolved(base_sha, head_sha, "base_commit_missing_from_history")
            for commit in page:
                if not isinstance(commit, dict):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                sha = commit.get("sha")
                author = (commit.get("commit") or {}).get("author")
                name = author.get("name") if isinstance(author, dict) else None
                if not isinstance(sha, str) or not _FULL_SHA.fullmatch(sha):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                if sha == base_sha:
                    if len(records) != ahead_by:
                        return _unresolved(base_sha, head_sha, "range_identity_mismatch")
                    offenders = [
                        {"sha": record["sha"][:12], "author": record["author"]}
                        for record in records
                        if record["author"].casefold() != BOT_COMMIT_AUTHOR.casefold()
                    ][:MAX_REPORTED_OFFENDERS]
                    return {
                        "state": "resolved",
                        "count": sum(
                            record["author"].casefold() != BOT_COMMIT_AUTHOR.casefold()
                            for record in records
                        ),
                        "offenders": offenders,
                        "range": {"base_sha": base_sha, "head_sha": head_sha},
                    }
                if not isinstance(name, str):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                records.append({"sha": sha, "author": name})
                if len(records) > ahead_by:
                    return _unresolved(base_sha, head_sha, "range_identity_mismatch")
            page_number += 1
    except Exception:  # noqa: BLE001 - network/API/schema failures are unresolved
        return _unresolved(base_sha, head_sha, "github_history_unavailable")


async def count_non_bot_authored_commits(
    base_sha: str | None,
    head_sha: str | None,
    *,
    repo_root: str | Path | None = None,
    session: Any | None = None,
    project: Any | None = None,
) -> dict[str, Any]:
    """Count non-bot authors in ``base_sha..head_sha`` without trusting a zero.

    The measurement is deliberately fail-closed. Missing refs, a non-ancestor
    base, unauthenticated GitHub access, and incomplete history return
    ``state=unresolved`` with ``count=None``.  The explicit local-checkout path
    runs in a worker thread so subprocess I/O never blocks the async event loop.
    """
    if not (
        isinstance(base_sha, str)
        and isinstance(head_sha, str)
        and _FULL_SHA.fullmatch(base_sha)
        and _FULL_SHA.fullmatch(head_sha)
    ):
        return _unresolved(base_sha, head_sha, "missing_or_malformed_sha")
    if repo_root is not None:
        return await asyncio.to_thread(
            _count_non_bot_authored_commits_from_git,
            base_sha,
            head_sha,
            repo_root=repo_root,
        )
    if session is None or project is None:
        return _unresolved(base_sha, head_sha, "github_context_unavailable")
    return await _count_non_bot_authored_commits_from_github(
        base_sha, head_sha, session=session, project=project
    )
