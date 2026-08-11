"""Tests for GitHub-backed release authorship evidence and its projection."""

from __future__ import annotations

from datetime import datetime, timezone
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


def _github_commit(
    sha: str,
    author: str,
    *,
    login: str | None = None,
    identity_type: str | None = None,
    verified: bool | None = None,
) -> dict:
    commit = {"sha": sha, "commit": {"author": {"name": author}}}
    if login is not None or identity_type is not None:
        commit["author"] = {"login": login, "type": identity_type}
    if verified is not None:
        commit["commit"]["verification"] = {"verified": verified}
    return commit


def _compare(base: str, head: str, commits: list[dict], *, total: int | None = None, **extra) -> dict:
    total = len(commits) if total is None else total
    return {
        "status": "identical" if total == 0 else "ahead",
        "ahead_by": total,
        "total_commits": total,
        "merge_base_commit": {"sha": base},
        "head_commit": {"sha": head},
        "commits": commits,
        **extra,
    }


@pytest.mark.asyncio
async def test_production_default_resolves_without_a_checkout(monkeypatch):
    """The deployed call shape has no repo_root and resolves through GitHub."""
    from app import github

    base, human = "a" * 40, "b" * 40
    project = Project(id="release-authorship-production", github_owner="owner", github_repo="repo")
    requests: list[dict] = []

    async def _request(method, url, headers, **kwargs):
        requests.append({"method": method, "url": url, "headers": headers, **kwargs})
        return _GitHubResponse(200, _compare(base, human, [_github_commit(human, "Henry Example")]))

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
    assert all("/compare/" in request["url"] for request in requests)
    assert all("/commits" not in request["url"] for request in requests)
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
        AsyncMock(return_value=_GitHubResponse(200, {"status": "diverged"})),
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
async def test_compare_paginates_101_commits_and_requires_unique_complete_pages(monkeypatch):
    from app import github

    base, head = "a" * 40, "b" * 40
    commits = [
        _github_commit(
            f"{index:040x}",
            "blueprintagentorchestrator[bot]",
            login="blueprintagentorchestrator[bot]",
            identity_type="Bot",
            verified=True,
        )
        for index in range(1, 102)
    ]
    project = Project(id="release-authorship-pages", github_owner="owner", github_repo="repo")
    requests = []

    async def _request(method, url, headers, **kwargs):
        requests.append(kwargs["params"])
        page = kwargs["params"]["page"]
        page_commits = commits[(page - 1) * 100 : page * 100]
        return _GitHubResponse(200, _compare(base, head, page_commits, total=101))

    monkeypatch.setattr("app.release_promotion._resolve_github_with_session", AsyncMock(return_value=("token", "owner", "repo")))
    monkeypatch.setattr(github, "_github_request", _request)

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "resolved"
    assert report["count"] == 0
    assert requests == [{"per_page": 100, "page": 1}, {"per_page": 100, "page": 2}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda body, base, head: body.update({"merge_base_commit": {"sha": "c" * 40}}), "malformed_github_range"),
        (lambda body, base, head: body.update({"head_commit": {"sha": "c" * 40}}), "malformed_github_range"),
        (lambda body, base, head: body.update({"total_commits": 2}), "malformed_github_range"),
        (
            lambda body, base, head: body.update(
                {
                    "ahead_by": 2,
                    "total_commits": 2,
                    "commits": [body["commits"][0], body["commits"][0]],
                }
            ),
            "github_history_duplicate",
        ),
    ],
)
async def test_malformed_compare_evidence_stays_unresolved(monkeypatch, mutate, reason):
    from app import github

    base, head = "a" * 40, "b" * 40
    body = _compare(base, head, [_github_commit(head, "Henry Example")])
    mutate(body, base, head)
    project = Project(id="release-authorship-malformed-evidence", github_owner="owner", github_repo="repo")
    monkeypatch.setattr("app.release_promotion._resolve_github_with_session", AsyncMock(return_value=("token", "owner", "repo")))
    monkeypatch.setattr(github, "_github_request", AsyncMock(return_value=_GitHubResponse(200, body)))

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "unresolved"
    assert report["reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit",
    [
        _github_commit("b" * 40, "blueprintagentorchestrator[bot]", verified=True),
        _github_commit("b" * 40, "blueprintagentorchestrator[bot]", login="other[bot]", identity_type="Bot", verified=True),
        _github_commit("b" * 40, "blueprintagentorchestrator[bot]", login="blueprintagentorchestrator[bot]", identity_type="User", verified=True),
        _github_commit("b" * 40, "blueprintagentorchestrator[bot]", login="blueprintagentorchestrator[bot]", identity_type="Bot", verified=False),
    ],
)
async def test_bot_shaped_commit_requires_full_verified_github_identity(monkeypatch, commit):
    from app import github

    base, head = "a" * 40, "b" * 40
    project = Project(id="release-authorship-bot-identity", github_owner="owner", github_repo="repo")
    monkeypatch.setattr("app.release_promotion._resolve_github_with_session", AsyncMock(return_value=("token", "owner", "repo")))
    monkeypatch.setattr(github, "_github_request", AsyncMock(return_value=_GitHubResponse(200, _compare(base, head, [commit]))))

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "resolved"
    assert report["count"] == 1
    assert report["offenders"] == [{"sha": head[:12], "author": "blueprintagentorchestrator[bot]"}]


@pytest.mark.asyncio
async def test_fully_verified_blueprint_bot_commit_is_not_an_offender(monkeypatch):
    from app import github

    base, head = "a" * 40, "b" * 40
    trusted_bot = _github_commit(
        head,
        "blueprintagentorchestrator[bot]",
        login="blueprintagentorchestrator[bot]",
        identity_type="Bot",
        verified=True,
    )
    project = Project(id="release-authorship-trusted-bot", github_owner="owner", github_repo="repo")
    monkeypatch.setattr("app.release_promotion._resolve_github_with_session", AsyncMock(return_value=("token", "owner", "repo")))
    monkeypatch.setattr(github, "_github_request", AsyncMock(return_value=_GitHubResponse(200, _compare(base, head, [trusted_bot]))))

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "resolved"
    assert report["count"] == 0
    assert report["offenders"] == []


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
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        opened_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
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
    assert response.json()["operator_authored_commits"]["range"] == {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
    }
    count.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_confirmation_receipt_preserves_serving_truth(db_session, mocker):
    from app.release_commit_authorship import persist_serving_authorship_evidence

    project = Project(name="Authorship outage", user_id="authorship-outage")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        base_sha="a" * 40,
        served_sha="b" * 40,
        serving_status="serving_confirmed",
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_qualification={"status": "promoted"},
    )
    db_session.add_all([project, version])
    await db_session.commit()
    await db_session.refresh(version)
    mocker.patch(
        "app.release_commit_authorship.count_non_bot_authored_commits",
        new=AsyncMock(side_effect=RuntimeError("GitHub outage")),
    )

    await persist_serving_authorship_evidence(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == "b" * 40
    assert version.last_qualification["operator_authored_commits"] == {
        "state": "unresolved",
        "count": None,
        "offenders": [],
        "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
        "reason": "github_authorship_unavailable",
    }


@pytest.mark.asyncio
async def test_promotion_defers_authorship_until_serving_confirmation(db_session, mocker):
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

    measurement = mocker.patch(
        "app.release_commit_authorship.count_non_bot_authored_commits",
        new=AsyncMock(side_effect=AssertionError("promotion must not measure serving evidence")),
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
    assert "operator_authored_commits" not in version.last_qualification
    measurement.assert_not_awaited()
