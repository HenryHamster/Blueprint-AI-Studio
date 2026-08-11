"""Tests for GitHub-backed release authorship evidence and its projection."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.models import Project, ReleaseVersion, Task
from app.release_commit_authorship import count_non_bot_authored_commits


class _GitHubResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _github_commit(sha: str, author: str) -> dict:
    return {"sha": sha, "commit": {"author": {"name": author}}}


@pytest.mark.asyncio
async def test_production_default_resolves_without_a_checkout(monkeypatch):
    """The deployed call shape has no repo_root and resolves through GitHub."""
    from app import github

    base, human = "a" * 40, "b" * 40
    project = Project(id="release-authorship-production", github_owner="owner", github_repo="repo")
    requests: list[dict] = []

    async def _request(method, url, headers, **kwargs):
        requests.append({"method": method, "url": url, "headers": headers, **kwargs})
        if "/compare/" in url:
            return _GitHubResponse(200, {"status": "ahead", "ahead_by": 1})
        return _GitHubResponse(200, [_github_commit(human, "Henry Example"), _github_commit(base, "Base")])

    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(github, "_github_request", _request)

    report = await count_non_bot_authored_commits(
        base, human, session=object(), project=project
    )

    assert report["state"] == "resolved"
    assert report["count"] == 1
    assert report["offenders"] == [{"sha": human[:12], "author": "Henry Example"}]
    assert all("repo_root" not in request for request in requests)


@pytest.mark.asyncio
async def test_compare_api_failure_stays_unresolved(monkeypatch):
    from app import github

    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-failure", github_owner="owner", github_repo="repo")
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
async def test_missing_credentials_stays_unresolved(monkeypatch):
    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-credentials", github_owner="owner", github_repo="repo")
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=None),
    )

    report = await count_non_bot_authored_commits(
        base, head, session=object(), project=project
    )

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["reason"] == "github_credentials_unavailable"


@pytest.mark.asyncio
async def test_non_ancestor_compare_stays_unresolved(monkeypatch):
    from app import github

    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-ancestor", github_owner="owner", github_repo="repo")
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
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
async def test_malformed_compare_range_stays_unresolved(monkeypatch):
    from app import github

    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-malformed", github_owner="owner", github_repo="repo")
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


@pytest.mark.asyncio
async def test_promotion_measurement_exception_is_unresolved_not_promotion_failure(
    mocker,
):
    from app.release_promotion import _measure_promotion_authorship

    mocker.patch(
        "app.release_commit_authorship.count_non_bot_authored_commits",
        new=AsyncMock(side_effect=RuntimeError("temporary GitHub outage")),
    )
    report = await _measure_promotion_authorship(
        object(),
        Project(id="release-authorship-promotion-failure", github_owner="owner", github_repo="repo"),
        "a" * 40,
        "b" * 40,
    )

    assert report["state"] == "unresolved"
    assert report["count"] is None
    assert report["range"] == {"base_sha": "a" * 40, "head_sha": "b" * 40}
    assert report["reason"] == "github_authorship_unavailable"


@pytest.mark.asyncio
async def test_read_path_serves_persisted_report_without_recomputation(
    client: AsyncClient, db_session, monkeypatch
):
    project = Project(name="Persisted Authorship", user_id="authorship-read")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="authorship-read-vm")
    promoted = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        opened_at=datetime(2026, 1, 1),
        last_qualification={
            "status": "promoted",
            "operator_authored_commits": {
                "state": "resolved",
                "count": 0,
                "offenders": [],
                "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
            },
        },
    )
    open_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        status="open",
        base_sha="b" * 40,
        opened_at=datetime(2026, 2, 1),
    )
    db_session.add_all([task, promoted, open_version])
    await db_session.commit()
    await db_session.refresh(task)

    monkeypatch.setattr("app.deployed_sha.get_serving_sha", lambda: "c" * 40)
    count = AsyncMock(side_effect=AssertionError("read path recomputed authorship"))
    monkeypatch.setattr("app.release_commit_authorship.count_non_bot_authored_commits", count)

    response = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": create_agent_token(str(task.id), task.vm_instance_id)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["operator_authored_commits"]["count"] == 0
    count.assert_not_awaited()


@pytest.mark.asyncio
async def test_promotion_persists_authorship_report(db_session, mocker):
    from app import release_version
    from app.release_promotion import _record_promotion_success

    project = Project(
        name="Promotion Authorship",
        user_id="authorship-promotion",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await release_version.open_version(
        db_session, project.id, version_number=1, base_sha="a" * 40
    )
    await db_session.commit()
    await db_session.refresh(version)

    report = {
        "state": "resolved",
        "count": 1,
        "offenders": [{"sha": "b" * 12, "author": "Henry Example"}],
        "range": {"base_sha": "a" * 40, "head_sha": "c" * 40},
    }
    measurement = mocker.patch(
        "app.release_commit_authorship.count_non_bot_authored_commits",
        new=AsyncMock(return_value=report),
    )
    mocker.patch(
        "app.compute_backends.refresh_project_compute_image",
        new=AsyncMock(return_value={"status": "unchanged"}),
    )

    result = await _record_promotion_success(
        db_session,
        version,
        candidate_sha="d" * 40,
        base_record={"confidence": 0.9},
        pr_url="https://github.com/owner/repo/pull/1",
        promoted_sha="c" * 40,
        lock_released=True,
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "promoted"
    assert version.last_qualification["operator_authored_commits"] == report
    measurement.assert_awaited_once()
    assert measurement.await_args.kwargs["project"].id == project.id
    assert measurement.await_args.args == ("a" * 40, "c" * 40)
