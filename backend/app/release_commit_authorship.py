"""Fail-closed measurement of operator-authored commits in a release range.

The deployed backend image intentionally has no checkout or ``git`` binary.
Production callers therefore resolve release history through the authenticated
GitHub API; no local-checkout fallback is supported.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.models import Project


BOT_COMMIT_AUTHOR = "blueprintagentorchestrator[bot]"
MAX_REPORTED_OFFENDERS = 10
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_COMMITS_PER_PAGE = 100
_MAX_COMPARE_COMMITS = 10_000
_AUTHORSHIP_MEASUREMENT_TIMEOUT_SECONDS = 30


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


async def _count_non_bot_authored_commits_from_github(
    base_sha: str,
    head_sha: str,
    *,
    session: Any,
    project: Any,
) -> dict[str, Any]:
    """Resolve the exact immutable release range through GitHub's API.

    Every evidence page comes from GitHub's ``compare/base...head`` endpoint.
    That keeps ancestry, merge base, head identity, total size, and commit
    authorship in one immutable range contract; ``/commits?sha=HEAD`` only
    proves a moving branch walk and must never be used as release evidence.
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
        expected_total: int | None = None
        records: list[dict[str, str]] = []
        seen_shas: set[str] = set()
        page_number = 1
        while True:
            response = await github._github_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
                headers,
                params={"per_page": _COMMITS_PER_PAGE, "page": page_number},
                project_id=project_id,
                consumer="release_commit_authorship",
            )
            if response.status_code != 200:
                return _unresolved(base_sha, head_sha, "github_range_unavailable")
            comparison_data = response.json()
            if not isinstance(comparison_data, dict):
                return _unresolved(base_sha, head_sha, "malformed_github_range")
            status = comparison_data.get("status")
            ahead_by = comparison_data.get("ahead_by")
            total_commits = comparison_data.get("total_commits")
            merge_base = (comparison_data.get("merge_base_commit") or {}).get("sha")
            returned_head = (comparison_data.get("head_commit") or {}).get("sha")
            if status not in {"identical", "ahead"}:
                return _unresolved(base_sha, head_sha, "range_not_ancestor")
            if (
                isinstance(ahead_by, bool)
                or not isinstance(ahead_by, int)
                or ahead_by < 0
                or isinstance(total_commits, bool)
                or not isinstance(total_commits, int)
                or total_commits != ahead_by
                or not isinstance(merge_base, str)
                or merge_base != base_sha
                or not isinstance(returned_head, str)
                or returned_head != head_sha
            ):
                return _unresolved(base_sha, head_sha, "malformed_github_range")
            if (
                (status == "identical" and (total_commits != 0 or base_sha != head_sha))
                or (status == "ahead" and total_commits == 0)
            ):
                return _unresolved(base_sha, head_sha, "range_identity_mismatch")
            if total_commits > _MAX_COMPARE_COMMITS:
                return _unresolved(base_sha, head_sha, "github_range_too_large")
            if expected_total is None:
                expected_total = total_commits
            elif expected_total != total_commits:
                return _unresolved(base_sha, head_sha, "range_pagination_mismatch")
            page = comparison_data.get("commits")
            if not isinstance(page, list):
                return _unresolved(base_sha, head_sha, "malformed_github_history")
            expected_page_size = min(
                _COMMITS_PER_PAGE, expected_total - len(seen_shas)
            )
            if len(page) != expected_page_size:
                return _unresolved(base_sha, head_sha, "github_history_incomplete")
            for commit in page:
                if not isinstance(commit, dict):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                sha = commit.get("sha")
                author = (commit.get("commit") or {}).get("author")
                name = author.get("name") if isinstance(author, dict) else None
                if not isinstance(sha, str) or not _FULL_SHA.fullmatch(sha):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                if not isinstance(name, str):
                    return _unresolved(base_sha, head_sha, "malformed_github_history")
                if sha in seen_shas:
                    return _unresolved(base_sha, head_sha, "github_history_duplicate")
                seen_shas.add(sha)
                identity = commit.get("author")
                verification = (commit.get("commit") or {}).get("verification")
                trusted_bot = (
                    name.casefold() == BOT_COMMIT_AUTHOR.casefold()
                    and isinstance(identity, dict)
                    and identity.get("login") == BOT_COMMIT_AUTHOR
                    and identity.get("type") == "Bot"
                    and isinstance(verification, dict)
                    and verification.get("verified") is True
                )
                if not trusted_bot:
                    records.append({"sha": sha, "author": name})
            page_number += 1
            if len(seen_shas) == expected_total:
                break
            if page_number > (_MAX_COMPARE_COMMITS // _COMMITS_PER_PAGE) + 1:
                return _unresolved(base_sha, head_sha, "github_history_incomplete")
        if expected_total is None or len(seen_shas) != expected_total:
            return _unresolved(base_sha, head_sha, "github_history_incomplete")
        offenders = [
            {"sha": record["sha"][:12], "author": record["author"]}
            for record in records[:MAX_REPORTED_OFFENDERS]
        ]
        return {
            "state": "resolved",
            "count": len(records),
            "offenders": offenders,
            "range": {"base_sha": base_sha, "head_sha": head_sha},
        }
    except Exception:  # noqa: BLE001 - network/API/schema failures are unresolved
        return _unresolved(base_sha, head_sha, "github_history_unavailable")


async def count_non_bot_authored_commits(
    base_sha: str | None,
    head_sha: str | None,
    *,
    session: Any | None = None,
    project: Any | None = None,
) -> dict[str, Any]:
    """Count non-bot authors in ``base_sha..head_sha`` without trusting a zero.

    The measurement is deliberately fail-closed. Missing refs, a non-ancestor
    base, unauthenticated GitHub access, and incomplete history return
    ``state=unresolved`` with ``count=None``.
    """
    if not (
        isinstance(base_sha, str)
        and isinstance(head_sha, str)
        and _FULL_SHA.fullmatch(base_sha)
        and _FULL_SHA.fullmatch(head_sha)
    ):
        return _unresolved(base_sha, head_sha, "missing_or_malformed_sha")
    if session is None or project is None:
        return _unresolved(base_sha, head_sha, "github_context_unavailable")
    return await _count_non_bot_authored_commits_from_github(
        base_sha, head_sha, session=session, project=project
    )


async def persist_serving_authorship_evidence(session: Any, version: Any) -> None:
    """Persist one bounded authorship receipt for a confirmed serving range.

    Callers already hold the promoted-version row lock.  The range snapshot and
    the existing receipt range form a small CAS: an asynchronous GitHub result
    may only replace the receipt if the immutable range stayed unchanged.  An
    unresolved result is still evidence and never changes serving truth.
    """
    base_sha = version.base_sha
    served_sha = version.served_sha
    if version.serving_status != "serving_confirmed" or not isinstance(served_sha, str):
        return
    target_range = {"base_sha": base_sha, "head_sha": served_sha}
    before_qualification = dict(version.last_qualification or {})
    before_report = before_qualification.get("operator_authored_commits")
    before_range = (
        before_report.get("range") if isinstance(before_report, dict) else None
    )
    if before_range == target_range:
        return
    project = await session.get(Project, version.project_id)
    if project is None:
        report = _unresolved(base_sha, served_sha, "project_unavailable")
    else:
        try:
            report = await asyncio.wait_for(
                count_non_bot_authored_commits(
                    base_sha, served_sha, session=session, project=project
                ),
                timeout=_AUTHORSHIP_MEASUREMENT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            report = _unresolved(base_sha, served_sha, "github_authorship_timeout")
        except Exception:  # noqa: BLE001 - confirmation must remain truthful
            report = _unresolved(base_sha, served_sha, "github_authorship_unavailable")

    current = dict(version.last_qualification or {})
    current_report = current.get("operator_authored_commits")
    current_range = current_report.get("range") if isinstance(current_report, dict) else None
    if (
        version.serving_status != "serving_confirmed"
        or version.base_sha != base_sha
        or version.served_sha != served_sha
        or current_range != before_range
    ):
        return
    current["operator_authored_commits"] = report
    version.last_qualification = current
    flag_modified(version, "last_qualification")
