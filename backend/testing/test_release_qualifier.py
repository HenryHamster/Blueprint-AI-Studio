"""Regression tests for release qualification measurements."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.release_commit_authorship import (
    BOT_COMMIT_AUTHOR,
    count_non_bot_authored_commits,
)


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(root: Path, *, author: str) -> str:
    history = root / "history.txt"
    history.write_text(
        history.read_text(encoding="utf-8") + author + "\n"
        if history.exists()
        else author + "\n",
        encoding="utf-8",
    )
    _run_git(root, "add", "history.txt")
    _run_git(
        root,
        "-c",
        f"user.name={author}",
        "-c",
        "user.email=release-test@example.invalid",
        "commit",
        "-m",
        "release history fixture",
        "--author",
        f"{author} <release-test@example.invalid>",
    )
    return _run_git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run_git(root, "init", "--quiet")
    return root


@pytest.mark.asyncio
async def test_only_bot_commits_report_zero(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, author=BOT_COMMIT_AUTHOR)
    head = _commit(root, author=BOT_COMMIT_AUTHOR)

    report = await count_non_bot_authored_commits(base, head, repo_root=root)

    assert report["state"] == "resolved"
    assert report["count"] == 0
    assert report["offenders"] == []


@pytest.mark.asyncio
async def test_non_bot_commit_is_reported_with_short_sha_and_author(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, author=BOT_COMMIT_AUTHOR)
    human = _commit(root, author="Henry Example")

    report = await count_non_bot_authored_commits(base, human, repo_root=root)

    assert report["state"] == "resolved"
    assert report["count"] == 1
    assert report["offenders"] == [{"sha": human[:12], "author": "Henry Example"}]


@pytest.mark.asyncio
async def test_unresolvable_range_is_not_a_false_zero(tmp_path: Path):
    root = _repo(tmp_path)
    base = _commit(root, author=BOT_COMMIT_AUTHOR)

    report = await count_non_bot_authored_commits(base, "f" * 40, repo_root=root)

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "commit_not_resolved"


@pytest.mark.asyncio
async def test_shallow_repository_is_unresolved(tmp_path: Path):
    source = _repo(tmp_path)
    base = _commit(source, author=BOT_COMMIT_AUTHOR)
    _commit(source, author="Henry Example")
    shallow = tmp_path / "shallow"
    _run_git(
        tmp_path,
        "clone",
        "--quiet",
        "--depth=1",
        source.as_uri(),
        str(shallow),
    )
    head = _run_git(shallow, "rev-parse", "HEAD")

    report = await count_non_bot_authored_commits(base, head, repo_root=shallow)

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "shallow_repository"


class _GitHubResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _github_commit(sha: str, author: str) -> dict:
    return {"sha": sha, "commit": {"author": {"name": author}}}


@pytest.mark.asyncio
async def test_deployed_merge_range_resolves_humans_across_paginated_github_history(
    monkeypatch,
):
    """The no-checkout production path counts the exact API-proven merge range."""
    from app import github
    from app.models import Project

    base, oldest_human, bot, newest_human = (char * 40 for char in "abcd")
    project = Project(id="release-authorship-project", github_owner="owner", github_repo="repo")
    requests: list[dict] = []

    async def _request(method, url, headers, **kwargs):
        requests.append({"url": url, **kwargs})
        if "/compare/" in url:
            return _GitHubResponse(200, {"status": "ahead", "ahead_by": 3})
        page = kwargs["params"]["page"]
        if page == 1:
            return _GitHubResponse(
                200,
                [
                    _github_commit(newest_human, "Henry Example"),
                    _github_commit(bot, BOT_COMMIT_AUTHOR.upper()),
                ],
            )
        assert page == 2
        return _GitHubResponse(
            200,
            [_github_commit(oldest_human, "Ada Example"), _github_commit(base, "Base Author")],
        )

    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(github, "_github_request", _request)

    report = await count_non_bot_authored_commits(
        base,
        newest_human,
        session=object(),
        project=project,
    )

    assert report == {
        "state": "resolved",
        "count": 2,
        "offenders": [
            {"sha": newest_human[:12], "author": "Henry Example"},
            {"sha": oldest_human[:12], "author": "Ada Example"},
        ],
        "range": {"base_sha": base, "head_sha": newest_human},
    }
    history_pages = [
        request["params"]["page"] for request in requests if request["url"].endswith("/commits")
    ]
    assert history_pages == [1, 2]


@pytest.mark.asyncio
async def test_deployed_merge_range_api_failure_stays_unresolved(monkeypatch):
    """An unavailable authoritative range never becomes an optimistic zero."""
    from app import github
    from app.models import Project

    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-project", github_owner="owner", github_repo="repo")
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        github,
        "_github_request",
        AsyncMock(return_value=_GitHubResponse(503, {"message": "unavailable"})),
    )

    report = await count_non_bot_authored_commits(
        base, head, session=object(), project=project
    )

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "github_range_unavailable"


@pytest.mark.asyncio
async def test_deployed_merge_range_credentials_and_nonancestor_stay_unresolved(
    monkeypatch,
):
    """Missing credentials and a comparison that is not a release range fail closed."""
    from app import github
    from app.models import Project

    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-project", github_owner="owner", github_repo="repo")
    resolver = AsyncMock(return_value=None)
    monkeypatch.setattr("app.release_promotion._resolve_github_with_session", resolver)

    report = await count_non_bot_authored_commits(
        base, head, session=object(), project=project
    )

    assert report["state"] == "unresolved"
    assert report["reason"] == "github_credentials_unavailable"

    resolver.return_value = ("token", "owner", "repo")
    monkeypatch.setattr(
        github,
        "_github_request",
        AsyncMock(return_value=_GitHubResponse(200, {"status": "diverged", "ahead_by": 1})),
    )
    report = await count_non_bot_authored_commits(
        base, head, session=object(), project=project
    )

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "range_not_ancestor"


@pytest.mark.asyncio
async def test_deployed_merge_range_malformed_provenance_stays_unresolved(monkeypatch):
    """A comparison without a valid immutable range count cannot be classified."""
    from app import github
    from app.models import Project

    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-project", github_owner="owner", github_repo="repo")
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        github,
        "_github_request",
        AsyncMock(return_value=_GitHubResponse(200, {"status": "ahead", "ahead_by": "one"})),
    )

    report = await count_non_bot_authored_commits(
        base, head, session=object(), project=project
    )

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "malformed_github_range"
