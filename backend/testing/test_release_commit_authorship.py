"""Tests for GitHub-backed release authorship evidence and its projection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified

from app.auth import create_agent_token
from app.models import Project, ReleaseVersion, Task
from app.release_commit_authorship import (
    AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
    AUTHORSHIP_RECEIPT_SOURCE,
    count_non_bot_authored_commits,
    authorship_category_evidence,
    measure_serving_authorship_evidence,
    persist_serving_authorship_evidence,
    reconcile_serving_authorship_evidence,
    validate_authorship_receipt,
)
from app.release_scoring import evaluate_merge_grade


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
    raw_author = {"name": author}
    raw_committer = {"name": author}
    if login == "blueprintagentorchestrator[bot]":
        raw_author["email"] = (
            "272093198+blueprintagentorchestrator[bot]@users.noreply.github.com"
        )
        raw_committer["email"] = raw_author["email"]
    else:
        raw_author["email"] = "human@example.com"
        raw_committer["email"] = "human@example.com"
    commit = {
        "sha": sha,
        "commit": {"author": raw_author, "committer": raw_committer},
    }
    if login is not None or identity_type is not None:
        commit["author"] = {"login": login, "type": identity_type}
    if verified is not None:
        commit["commit"]["verification"] = {
            "verified": verified,
            "reason": "valid" if verified else "unsigned",
        }
    if login == "blueprintagentorchestrator[bot]":
        commit["author"]["id"] = 272093198
        commit["commit"]["committer"] = {
            "name": "GitHub",
            "email": "noreply@github.com",
        }
        commit["committer"] = {
            "login": "web-flow",
            "id": 19864447,
            "type": "User",
        }
    return commit


def _compare(base: str, head: str, commits: list[dict], *, total: int | None = None, **extra) -> dict:
    total = len(commits) if total is None else total
    return {
        "status": "identical" if total == 0 else "ahead",
        "ahead_by": total,
        "total_commits": total,
        "merge_base_commit": {"sha": base},
        "base_commit": {"sha": base},
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
    commits[-1]["sha"] = head
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
        (lambda body, base, head: body.update({"base_commit": {"sha": "c" * 40}}), "malformed_github_range"),
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
    ("status", "ahead_by", "total", "reason"),
    [
        ("identical", 1, 1, "range_identity_mismatch"),
        ("ahead", 0, 0, "range_identity_mismatch"),
    ],
)
async def test_compare_rejects_contradictory_identity_states(
    monkeypatch, status, ahead_by, total, reason
):
    from app import github

    base, head = "a" * 40, "b" * 40
    body = _compare(base, head, [_github_commit(head, "Henry Example")])
    body.update({"status": status, "ahead_by": ahead_by, "total_commits": total})
    if total == 0:
        body["commits"] = []
    project = Project(id="release-authorship-contradiction", github_owner="owner", github_repo="repo")
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        github, "_github_request", AsyncMock(return_value=_GitHubResponse(200, body))
    )

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "unresolved"
    assert report["reason"] == reason


@pytest.mark.asyncio
async def test_compare_rejects_base_in_commit_set(monkeypatch):
    from app import github

    base, head = "a" * 40, "b" * 40
    body = _compare(
        base,
        head,
        [_github_commit(base, "Base"), _github_commit(head, "Henry Example")],
    )
    project = Project(id="release-authorship-base-included", github_owner="owner", github_repo="repo")
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        github, "_github_request", AsyncMock(return_value=_GitHubResponse(200, body))
    )

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "unresolved"
    assert report["reason"] == "range_identity_mismatch"


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
async def test_direct_blueprint_bot_committer_requires_immutable_top_level_identity(monkeypatch):
    from app import github

    base, head = "a" * 40, "b" * 40
    direct = _github_commit(
        head,
        "blueprintagentorchestrator[bot]",
        login="blueprintagentorchestrator[bot]",
        identity_type="Bot",
        verified=True,
    )
    direct["commit"]["committer"] = {
        "name": "blueprintagentorchestrator[bot]",
        "email": "272093198+blueprintagentorchestrator[bot]@users.noreply.github.com",
    }
    direct["committer"] = {
        "login": "blueprintagentorchestrator[bot]",
        "id": 272093198,
        "type": "Bot",
    }
    project = Project(id="release-authorship-direct-bot", github_owner="owner", github_repo="repo")
    monkeypatch.setattr("app.release_promotion._resolve_github_with_session", AsyncMock(return_value=("token", "owner", "repo")))
    monkeypatch.setattr(github, "_github_request", AsyncMock(return_value=_GitHubResponse(200, _compare(base, head, [direct]))))

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "resolved"
    assert report["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("committer", [None, {"login": "web-flow", "id": 1, "type": "User"}])
async def test_merge_committer_null_or_wrong_id_is_not_trusted(monkeypatch, committer):
    from app import github

    base, head = "a" * 40, "b" * 40
    commit = _github_commit(
        head,
        "blueprintagentorchestrator[bot]",
        login="blueprintagentorchestrator[bot]",
        identity_type="Bot",
        verified=True,
    )
    commit["committer"] = committer
    project = Project(id="release-authorship-wrong-committer", github_owner="owner", github_repo="repo")
    monkeypatch.setattr("app.release_promotion._resolve_github_with_session", AsyncMock(return_value=("token", "owner", "repo")))
    monkeypatch.setattr(github, "_github_request", AsyncMock(return_value=_GitHubResponse(200, _compare(base, head, [commit]))))

    report = await count_non_bot_authored_commits(base, head, session=object(), project=project)

    assert report["state"] == "resolved"
    assert report["count"] == 1


@pytest.mark.asyncio
async def test_bot_looking_author_with_human_committer_is_an_offender(monkeypatch):
    """A human-signed commit cannot spoof the Blueprint App identity."""
    from app import github

    base, head = "a" * 40, "b" * 40
    spoof = _github_commit(
        head,
        "blueprintagentorchestrator[bot]",
        login="blueprintagentorchestrator[bot]",
        identity_type="Bot",
        verified=True,
    )
    spoof["commit"]["committer"] = {
        "name": "Henry Hamster",
        "email": "henry@example.com",
    }
    project = Project(id="release-authorship-human-committer", github_owner="owner", github_repo="repo")
    monkeypatch.setattr(
        "app.release_promotion._resolve_github_with_session",
        AsyncMock(return_value=("token", "owner", "repo")),
    )
    monkeypatch.setattr(
        github,
        "_github_request",
        AsyncMock(return_value=_GitHubResponse(200, _compare(base, head, [spoof]))),
    )

    report = await count_non_bot_authored_commits(
        base, head, session=object(), project=project
    )

    assert report["state"] == "resolved"
    assert report["count"] == 1
    assert report["offenders"] == [
        {"sha": head[:12], "author": "blueprintagentorchestrator[bot]"}
    ]


def test_receipt_rejects_tampered_zero_and_offender_shapes():
    base, head = "a" * 40, "b" * 40
    common = {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "resolved",
        "count": 1,
        "offenders": [{"sha": "b" * 12, "author": "Human"}],
        "range": {"base_sha": base, "head_sha": head},
        "measured_at": "2026-01-01T00:00:00+00:00",
        "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
    }
    assert validate_authorship_receipt(common, base, head) is not None
    assert validate_authorship_receipt({**common, "count": 0}, base, head) is None
    assert validate_authorship_receipt(
        {**common, "offenders": [{"sha": "B" * 12, "author": "Human"}]},
        base,
        head,
    ) is None
    assert validate_authorship_receipt(
        {**common, "offenders": [{"sha": "b" * 12, "author": "Human", "extra": 1}]},
        base,
        head,
    ) is None


def test_authorship_receipt_drives_normal_category_grader_without_seeded_verdict():
    base, head = "a" * 40, "b" * 40
    category = [{
        "category_key": "generated-authorship-key",
        "text": "All release commits must satisfy the Blueprint bot authorship contract.",
    }]
    resolved = {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "resolved",
        "count": 0,
        "offenders": [],
        "range": {"base_sha": base, "head_sha": head},
        "measured_at": "2026-01-01T00:00:00+00:00",
        "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
    }
    evidence = authorship_category_evidence(resolved, base, head, category)
    grade = evaluate_merge_grade(
        "release goal",
        {"covers_goal": True, "category_verdicts": []},
        category,
        authoritative_category_evidence=evidence,
    )

    assert grade["category_verdicts"] == [{
        "category_key": "generated-authorship-key",
        "status": "met",
        "tier": "machine",
        "gap": None,
        "citations": [{"kind": "diff", "ref": "release-authorship:count=0"}],
    }]


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"schema_version": 1, "state": "resolved"},
        {
            "source": AUTHORSHIP_RECEIPT_SOURCE,
            "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
            "state": "unresolved",
            "count": None,
            "offenders": [],
            "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
            "reason": "github_authorship_timeout",
            "measured_at": "2026-01-01T00:00:00+00:00",
            "retry": {"attempt": 1, "retryable": True, "next_retry_at": "2026-01-02T00:00:00+00:00"},
        },
    ],
)
def test_missing_legacy_or_unresolved_authorship_evidence_fails_category_closed(receipt):
    category = [{"category_key": "generated-authorship-key", "text": "Commit authorship is Blueprint-controlled."}]
    evidence = authorship_category_evidence(receipt, "a" * 40, "b" * 40, category)

    assert evidence[0]["status"] == "not_met"
    assert evidence[0]["authoritative"] is True
    assert evidence[0]["citations"]


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
        base_sha="a" * 40,
        served_sha="b" * 40,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        serving_status="serving_confirmed",
        last_qualification={
            "status": "promoted",
            "operator_authored_commits": {
                "source": AUTHORSHIP_RECEIPT_SOURCE,
                "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
                "state": "resolved",
                "count": 0,
                "offenders": [],
                "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
                "measured_at": "2026-01-01T00:00:00+00:00",
                "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
            },
        },
    )
    open_version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        status="open",
        base_sha="b" * 40,
        opened_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        last_qualification={
        },
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

    # A read must fail closed if the stored JSON is tampered with, even though
    # the range still names the correct promoted release.
    tampered_qualification = dict(promoted.last_qualification)
    tampered_report = dict(tampered_qualification["operator_authored_commits"])
    tampered_report["range"] = {"base_sha": "a" * 40, "head_sha": "c" * 40}
    tampered_qualification["operator_authored_commits"] = tampered_report
    promoted.last_qualification = tampered_qualification
    flag_modified(promoted, "last_qualification")
    db_session.add(promoted)
    await db_session.commit()
    await db_session.refresh(promoted)
    assert promoted.last_qualification["operator_authored_commits"]["range"]["head_sha"] == "c" * 40
    tampered = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": create_agent_token(str(task.id), task.vm_instance_id)},
    )
    assert tampered.json()["operator_authored_commits"]["state"] == "unresolved"
    assert tampered.json()["operator_authored_commits"]["reason"] == (
        "stored_authorship_receipt_invalid"
    )


@pytest.mark.asyncio
async def test_retryable_legacy_receipt_reconciles_and_resolves(db_session, mocker):
    project = Project(name="Authorship retry", user_id="authorship-retry")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        base_sha="a" * 40,
        served_sha="b" * 40,
        serving_status="serving_confirmed",
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_qualification={
            "operator_authored_commits": {
                "state": "unresolved",
                "count": None,
                "offenders": [],
                "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
                "reason": "github_authorship_timeout",
            }
        },
    )
    db_session.add_all([project, version])
    await db_session.commit()
    await db_session.refresh(version)
    resolved = {
        "state": "resolved",
        "count": 1,
        "offenders": [{"sha": "b" * 12, "author": "Henry Example"}],
        "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
    }
    measure = mocker.patch(
        "app.release_commit_authorship.count_non_bot_authored_commits",
        new=AsyncMock(side_effect=[
            {"state": "unresolved", "count": None, "offenders": [], "range": resolved["range"], "reason": "temporary"},
            resolved,
        ]),
    )

    first_plan = await persist_serving_authorship_evidence(db_session, version)
    await db_session.commit()
    await measure_serving_authorship_evidence(
        version.id,
        expected_receipt_identity=first_plan["receipt_identity"],
    )
    await db_session.refresh(version)
    assert version.last_qualification["operator_authored_commits"]["state"] == "unresolved"
    assert version.last_qualification["operator_authored_commits"]["retry"]["attempt"] == 1

    version.last_qualification["operator_authored_commits"]["retry"]["next_retry_at"] = (
        "2025-01-01T00:00:00+00:00"
    )
    flag_modified(version, "last_qualification")
    await db_session.commit()

    second_plan = await persist_serving_authorship_evidence(db_session, version)
    await db_session.commit()
    await measure_serving_authorship_evidence(
        version.id,
        expected_receipt_identity=second_plan["receipt_identity"],
    )
    await db_session.refresh(version)
    receipt = version.last_qualification["operator_authored_commits"]
    assert receipt["state"] == "resolved"
    assert receipt["count"] == 1
    assert receipt["retry"]["attempt"] == 2
    assert receipt["retry"]["retryable"] is False
    assert receipt["retry"]["next_retry_at"] is None
    assert measure.await_count == 2


@pytest.mark.asyncio
async def test_resolved_current_receipt_is_immutable_and_short_circuits(db_session, mocker):
    project = Project(name="Authorship immutable", user_id="authorship-immutable")
    receipt = {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "resolved",
        "count": 0,
        "offenders": [],
        "range": {"base_sha": "a" * 40, "head_sha": "b" * 40},
        "measured_at": "2026-01-01T00:00:00+00:00",
        "retry": {"attempt": 2, "retryable": False, "next_retry_at": None},
    }
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        base_sha="a" * 40,
        served_sha="b" * 40,
        serving_status="serving_confirmed",
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_qualification={"operator_authored_commits": receipt},
    )
    db_session.add_all([project, version])
    await db_session.commit()
    await db_session.refresh(version)
    measure = mocker.patch(
        "app.release_commit_authorship.count_non_bot_authored_commits",
        new=AsyncMock(side_effect=AssertionError("resolved receipt must be immutable")),
    )

    await persist_serving_authorship_evidence(db_session, version)

    await db_session.refresh(version)
    assert version.last_qualification["operator_authored_commits"] == receipt
    measure.assert_not_awaited()


@pytest.mark.asyncio
async def test_staging_respects_terminal_immutability_and_future_backoff(db_session):
    base, head = "a" * 40, "b" * 40
    terminal = {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "terminal",
        "count": None,
        "offenders": [],
        "range": {"base_sha": base, "head_sha": head},
        "reason": "provenance_untrusted",
        "measured_at": "2026-01-01T00:00:00+00:00",
        "retry": {"attempt": 2, "retryable": False, "next_retry_at": None},
    }
    future = {
        **terminal,
        "state": "unresolved",
        "reason": "github_authorship_timeout",
        "retry": {
            "attempt": 2,
            "retryable": True,
            "next_retry_at": "2099-01-01T00:00:00+00:00",
        },
    }
    for number, receipt in enumerate((terminal, future), start=1):
        project = Project(name=f"Authorship staging {number}", user_id=f"staging-{number}")
        version = ReleaseVersion(
            project_id=project.id,
            version_number=number,
            status="promoted",
            base_sha=base,
            served_sha=head,
            serving_status="serving_confirmed",
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification={"operator_authored_commits": receipt},
        )
        db_session.add_all([project, version])
        await db_session.commit()
        await db_session.refresh(version)

        assert await persist_serving_authorship_evidence(db_session, version) is None
        if receipt["state"] == "unresolved":
            assert await persist_serving_authorship_evidence(db_session, version, force=True)
        await db_session.rollback()


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

    plan = await persist_serving_authorship_evidence(db_session, version)
    await db_session.commit()
    await measure_serving_authorship_evidence(
        version.id,
        expected_receipt_identity=plan["receipt_identity"],
    )
    await db_session.refresh(version)

    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == "b" * 40
    receipt = version.last_qualification["operator_authored_commits"]
    assert receipt["source"] == AUTHORSHIP_RECEIPT_SOURCE
    assert receipt["schema_version"] == AUTHORSHIP_RECEIPT_SCHEMA_VERSION
    assert receipt["state"] == "unresolved"
    assert receipt["reason"] == "github_authorship_unavailable"
    assert receipt["range"] == {"base_sha": "a" * 40, "head_sha": "b" * 40}
    assert receipt["retry"]["attempt"] == 1
    assert receipt["retry"]["retryable"] is True
    assert receipt["retry"]["next_retry_at"] is not None


@pytest.mark.asyncio
async def test_staging_never_commits_caller_transaction(db_session, mocker):
    project = Project(name="Authorship transaction", user_id="authorship-transaction")
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        base_sha="a" * 40,
        served_sha="b" * 40,
        serving_status="serving_confirmed",
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_qualification={},
    )
    db_session.add_all([project, version])
    await db_session.commit()
    await db_session.refresh(version)
    commit = mocker.patch.object(db_session, "commit", new=AsyncMock())

    plan = await persist_serving_authorship_evidence(db_session, version)

    assert plan is not None
    commit.assert_not_awaited()
    assert "category_verdicts" not in version.last_qualification
    await db_session.rollback()


@pytest.mark.asyncio
async def test_reconciliation_old_rows_and_concurrent_claim_once(db_session, mocker):
    from app.database import AsyncSessionLocal

    projects = [Project(name=f"Sweep {i}", user_id=f"sweep-{i}") for i in range(2)]
    db_session.add_all(projects)
    await db_session.commit()
    for project in projects:
        await db_session.refresh(project)
    resolved = []
    for i in range(4):
        base, head = f"{i + 1:040x}", f"{i + 10:040x}"
        resolved.append(
            ReleaseVersion(
                project_id=projects[i % 2].id,
                version_number=i + 1,
                status="promoted",
                base_sha=base,
                served_sha=head,
                serving_status="serving_confirmed",
                served_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_qualification={"operator_authored_commits": {
                    "source": AUTHORSHIP_RECEIPT_SOURCE,
                    "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
                    "state": "resolved", "count": 0, "offenders": [],
                    "range": {"base_sha": base, "head_sha": head},
                    "measured_at": "2026-01-01T00:00:00+00:00",
                    "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
                }},
            )
        )
    target = ReleaseVersion(
        project_id=projects[0].id, version_number=999, status="promoted",
        base_sha="f" * 40, served_sha="e" * 40,
        serving_status="serving_confirmed",
        served_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        opened_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    db_session.add_all([*resolved, target])
    await db_session.commit()
    await db_session.refresh(target)

    async def _slow_measure(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return True

    measure = mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(side_effect=_slow_measure),
    )
    results = await asyncio.gather(
        reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1),
        reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1),
    )

    assert sorted(results) == [0, 1]
    assert measure.await_count == 1
    assert measure.await_args.args[0] == target.id


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
