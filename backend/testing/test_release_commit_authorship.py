"""Tests for GitHub-backed release authorship evidence and its projection."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified
from testing.conftest import USE_POSTGRES

from app.auth import create_agent_token
from app.models import Project, ReleaseVersion, Task
from app.release_commit_authorship import (
    AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
    AUTHORSHIP_RECEIPT_SOURCE,
    count_non_bot_authored_commits,
    authorship_category_evidence,
    measure_serving_authorship_evidence,
    normalized_authorship_prompt_evidence,
    persist_serving_authorship_evidence,
    reconcile_serving_authorship_evidence,
    validate_authorship_receipt,
)
from app.release_commit_authorship import _deadline_session
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
        "evidence_contract": {
            "type": "operator_authored_commits",
            "predicate": {"max_count": 0},
        },
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
        "evidence_provenance": {
            "kind": "machine",
            "source": "release_authorship",
            "source_kind": "github_compare",
            "receipt_source": "github_compare",
            "receipt_state": "resolved",
            "receipt_measured_at": "2026-01-01T00:00:00+00:00",
            "receipt_attempt": 1,
            "range": {"base_sha": base, "head_sha": head},
        },
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
    category = [{
        "category_key": "generated-authorship-key",
        "text": "Commit authorship is Blueprint-controlled.",
        "evidence_contract": {
            "type": "operator_authored_commits",
            "predicate": {"max_count": 0},
        },
    }]
    evidence = authorship_category_evidence(receipt, "a" * 40, "b" * 40, category)

    assert evidence[0]["status"] == "not_met"
    assert evidence[0]["authoritative"] is True
    assert evidence[0]["citations"]


def test_authorship_projection_requires_structured_contract_not_rubric_prose():
    base, head = "a" * 40, "b" * 40
    receipt = {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "resolved",
        "count": 0,
        "offenders": [],
        "range": {"base_sha": base, "head_sha": head},
        "measured_at": "2026-01-01T00:00:00+00:00",
        "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
    }
    categories = [
        {"category_key": "uncontracted", "text": "operator-authored commits"},
        {
            "category_key": "inverse",
            "text": "No operator-authored commits are required.",
            "evidence_contract": {
                "type": "some_other_contract",
                "predicate": {"max_count": 0},
            },
        },
    ]

    assert authorship_category_evidence(receipt, base, head, categories) == []


def test_authorship_contract_max_count_projects_nonzero_as_unmet_and_prompt_is_bounded():
    base, head = "a" * 40, "b" * 40
    receipt = {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "resolved",
        "count": 1,
        "offenders": [{"sha": "c" * 12, "author": "attacker-controlled name"}],
        "range": {"base_sha": base, "head_sha": head},
        "measured_at": "2026-01-01T00:00:00+00:00",
        "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
    }
    category = {
        "category_key": "generated-key",
        "text": "Any rubric prose is irrelevant to this projection.",
        "evidence_contract": {
            "type": "operator_authored_commits",
            "predicate": {"max_count": 0},
        },
    }

    evidence = authorship_category_evidence(receipt, base, head, [category])
    prompt = normalized_authorship_prompt_evidence(receipt, base, head, [category])

    assert evidence[0]["status"] == "not_met"
    assert evidence[0]["citations"] == [{"kind": "diff", "ref": "release-authorship:cccccccccccc"}]
    assert "attacker-controlled name" not in str(prompt)
    assert prompt["operator_authored_commits"] == {
        "state": "resolved",
        "count": 1,
        "offender_shas": ["cccccccccccc"],
    }
    assert prompt["range"] == {"base_sha": "a" * 12, "head_sha": "b" * 12}


def test_malformed_receipt_is_repair_candidate_without_json_casting():
    from app.release_commit_authorship import _reconciliation_candidate_is_eligible

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for report in (
        {"schema_version": "2", "state": "resolved"},
        {"schema_version": 2, "state": "terminal", "retry": {"next_retry_at": []}},
        {"schema_version": 2, "state": "resolved", "measured_at": "not-a-time"},
        {"schema_version": 2, "state": "unresolved", "retry": {"next_retry_at": {}}},
    ):
        assert _reconciliation_candidate_is_eligible(report, "a" * 40, "b" * 40, now)


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
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="singleton row-lock contention requires the PostgreSQL harness",
)
async def test_reconciliation_old_rows_and_concurrent_claim_once(db_session, mocker):
    from app.database import AsyncSessionLocal
    from app.models import ReleaseAuthorshipReconciliationState

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
    db_session.add_all([*resolved, target, ReleaseAuthorshipReconciliationState(id=1)])
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
        reconcile_serving_authorship_evidence(lambda: AsyncSessionLocal(), limit=1),
        reconcile_serving_authorship_evidence(lambda: AsyncSessionLocal(), limit=1),
    )

    assert sorted(results) == [0, 1]
    assert measure.await_count == 1
    assert measure.await_args.args[0] == target.id


@pytest.mark.asyncio
async def test_reconciliation_cursor_is_durable_and_process_global_cursor_is_absent(
    db_session, mocker
):
    """A scan commits its keyset position in the singleton state row."""
    from app.database import AsyncSessionLocal
    from app.models import ReleaseAuthorshipReconciliationState
    from sqlalchemy import select

    project = Project(name="Durable cursor", user_id="durable-cursor")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    rows = [
        ReleaseVersion(
            project_id=project.id,
            version_number=index + 1,
            status="promoted",
            base_sha=f"{index + 1:040x}",
            served_sha=f"{index + 2:040x}",
            serving_status="serving_confirmed",
            served_at=None if index == 0 else datetime(2026, 1, 1, tzinfo=timezone.utc),
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification={},
        )
        for index in range(2)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    mocker.patch("app.release_commit_authorship._RECONCILIATION_SCAN_PAGE_SIZE", 1)
    mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(return_value=True),
    )

    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    state = (
        await db_session.execute(select(ReleaseAuthorshipReconciliationState).where(ReleaseAuthorshipReconciliationState.id == 1))
    ).scalar_one()
    assert state.cursor_partition == 0
    assert state.null_cursor_id == rows[0].id
    import app.release_commit_authorship as authorship

    assert not hasattr(authorship, "_reconciliation_cursor")

    # A later worker starts with a fresh in-process state and resumes from the
    # committed singleton cursor, reaching the next partition/page.
    measure = mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(return_value=True),
    )
    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    assert measure.await_args.args[0] == rows[1].id
    await db_session.refresh(state)
    assert state.cursor_partition == 1
    assert state.served_id_cursor == rows[1].id


@pytest.mark.asyncio
async def test_reconciliation_scan_cleanup_failure_terminalizes_sweep(db_session, mocker):
    """A scan cleanup failure cannot fall through to another page or worker."""
    from app.database import AsyncSessionLocal

    project = Project(name="Authorship scan cleanup", user_id="authorship-scan-cleanup")
    row = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        base_sha="a" * 40,
        served_sha="b" * 40,
        serving_status="serving_confirmed",
        served_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_qualification={},
    )
    db_session.add_all([project, row])
    await db_session.commit()

    class FailingExitContext:
        def __init__(self, context):
            self.context = context

        async def __aenter__(self):
            return await self.context.__aenter__()

        async def __aexit__(self, *exc_info):
            await self.context.__aexit__(*exc_info)
            raise RuntimeError("scan connection cleanup failed")

    measure = mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(return_value=True),
    )

    attempted = await reconcile_serving_authorship_evidence(
        lambda: FailingExitContext(AsyncSessionLocal()), limit=1
    )

    assert attempted == 0
    measure.assert_not_awaited()


@pytest.mark.asyncio
async def test_deadline_session_always_exits_and_propagates_cleanup_failure():
    """Uncertain cleanup is observable and cannot be followed by external work."""
    class FailingContext:
        def __init__(self):
            self.exit_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.exit_calls += 1
            raise RuntimeError("connection cleanup failed")

    context = FailingContext()

    def factory():
        return context

    with pytest.raises(RuntimeError, match="connection cleanup failed"):
        async with _deadline_session(factory, time.monotonic() + 1):
            raise asyncio.TimeoutError("operation budget exhausted")
    assert context.exit_calls == 1


@pytest.mark.asyncio
async def test_deadline_session_preserves_cancellation_when_cleanup_fails():
    """Cleanup uncertainty must not turn caller cancellation into a normal error."""
    class FailingContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            raise RuntimeError("connection cleanup failed")

    async def cancel_body():
        async with _deadline_session(FailingContext, time.monotonic() + 1):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError, match="") as caught:
        await cancel_body()
    assert any("cleanup failed while cancelled" in note for note in caught.value.__notes__)


@pytest.mark.asyncio
async def test_deadline_session_external_cancel_invalidates_before_owned_cleanup_reap():
    """External cancellation cannot leave a blocked driver cleanup unowned."""
    import app.release_commit_authorship as authorship

    cleanup_started = asyncio.Event()
    body_started = asyncio.Event()
    first_cleanup_cancel = asyncio.Event()
    release_cleanup = asyncio.Event()
    invalidated = asyncio.Event()
    factory_calls = 0

    class Session:
        async def invalidate(self):
            invalidated.set()

    session = Session()
    baseline_task_count = len(asyncio.all_tasks())

    class BlockingExit:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc_info):
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                # Resist the forced cancellation long enough to cross the old
                # 50ms branch; the caller must not regain control yet.
                first_cleanup_cancel.set()
                await release_cleanup.wait()
            return False

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return BlockingExit()

    async def use_session():
        async with _deadline_session(factory, time.monotonic() + 1):
            body_started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(use_session())
    await body_started.wait()
    task.cancel("caller cancellation")
    with pytest.raises(asyncio.CancelledError, match="caller cancellation"):
        await task

    assert factory_calls == 1
    assert invalidated.is_set(), "the checked-out connection must be invalidated"
    assert first_cleanup_cancel.is_set()
    assert authorship._RECONCILIATION_OWNED_CLEANUP_TASKS

    release_cleanup.set()
    for _ in range(5):
        await asyncio.sleep(0)
        if not authorship._RECONCILIATION_OWNED_CLEANUP_TASKS:
            break
    assert not authorship._RECONCILIATION_OWNED_CLEANUP_TASKS
    assert len(asyncio.all_tasks()) == baseline_task_count


@pytest.mark.asyncio
async def test_deadline_session_grace_expiry_invalidates_before_forced_cleanup_reap(
    mocker,
):
    """Ordinary grace expiry follows the same invalidate-then-own protocol."""
    import app.release_commit_authorship as authorship

    mocker.patch.object(authorship, "_RECONCILIATION_CLEANUP_GRACE_SECONDS", 0.01)
    mocker.patch.object(
        authorship, "_RECONCILIATION_CLEANUP_CANCEL_GRACE_SECONDS", 0.01
    )
    cleanup_started = asyncio.Event()
    first_cleanup_cancel = asyncio.Event()
    release_cleanup = asyncio.Event()
    invalidated = asyncio.Event()
    baseline_task_count = len(asyncio.all_tasks())

    class Session:
        async def invalidate(self):
            invalidated.set()

    class BlockingExit:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_exc_info):
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                first_cleanup_cancel.set()
                await release_cleanup.wait()
            return False

    with pytest.raises(authorship._ReconciliationCleanupError):
        async with _deadline_session(BlockingExit, time.monotonic() + 1):
            pass

    assert invalidated.is_set()
    assert first_cleanup_cancel.is_set()
    assert authorship._RECONCILIATION_OWNED_CLEANUP_TASKS
    release_cleanup.set()
    for _ in range(5):
        await asyncio.sleep(0)
        if not authorship._RECONCILIATION_OWNED_CLEANUP_TASKS:
            break
    assert not authorship._RECONCILIATION_OWNED_CLEANUP_TASKS
    assert len(asyncio.all_tasks()) == baseline_task_count


@pytest.mark.asyncio
async def test_reconciliation_cursor_wraps_and_revisits_newly_eligible_rows(
    db_session, mocker
):
    """Pages advance, wrap with a generation, and revisit rows behind the cursor."""
    import uuid

    from app.models import ReleaseAuthorshipReconciliationState
    from sqlalchemy import select
    from app.database import AsyncSessionLocal

    project = Project(name="Cursor wrap", user_id="cursor-wrap")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    resolved_report = {
        "source": AUTHORSHIP_RECEIPT_SOURCE,
        "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
        "state": "resolved",
        "count": 0,
        "offenders": [],
        "range": {"base_sha": f"{1:040x}", "head_sha": f"{10:040x}"},
        "measured_at": "2026-01-01T00:00:00+00:00",
        "retry": {"attempt": 1, "retryable": False, "next_retry_at": None},
    }
    null_rows = [
        ReleaseVersion(
            id=uuid.UUID(int=100 + index),
            project_id=project.id,
            version_number=index + 1,
            status="promoted",
            base_sha=f"{index + 1:040x}",
            served_sha=f"{index + 10:040x}",
            serving_status="serving_confirmed",
            served_at=None,
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification=(
                {"operator_authored_commits": resolved_report}
                if index == 0
                else {}
            ),
        )
        for index in range(3)
    ]
    served_rows = [
        ReleaseVersion(
            id=uuid.UUID(int=200 + index),
            project_id=project.id,
            version_number=index + 10,
            status="promoted",
            base_sha=f"{index + 20:040x}",
            served_sha=f"{index + 30:040x}",
            serving_status="serving_confirmed",
            served_at=datetime(2026, 1, index + 1, tzinfo=timezone.utc),
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification={},
        )
        for index in range(3)
    ]
    db_session.add_all([*null_rows, *served_rows])
    await db_session.commit()
    mocker.patch("app.release_commit_authorship._RECONCILIATION_SCAN_PAGE_SIZE", 2)
    measure = mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(return_value=True),
    )

    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    assert measure.await_args.args[0] == null_rows[1].id
    null_rows[0].last_qualification = {}
    flag_modified(null_rows[0], "last_qualification")
    await db_session.commit()

    measure.reset_mock()
    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    assert measure.await_args.args[0] == null_rows[2].id
    measure.reset_mock()
    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    assert measure.await_args.args[0] == served_rows[0].id
    measure.reset_mock()
    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    assert measure.await_args.args[0] == served_rows[1].id
    measure.reset_mock()
    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    assert measure.await_args.args[0] == served_rows[2].id
    measure.reset_mock()
    assert await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1) == 1
    assert measure.await_args.args[0] == null_rows[0].id

    state = (
        await db_session.execute(
            select(ReleaseAuthorshipReconciliationState).where(
                ReleaseAuthorshipReconciliationState.id == 1
            )
        )
    ).scalar_one()
    assert state.generation == 1, "EOF must increment the durable cursor generation"
    assert state.cursor_partition == 0


@pytest.mark.asyncio
async def test_migration_0755_to_0756_backfills_legacy_contract_audit_on_postgres():
    """The real Postgres chain preserves truthful legacy audit provenance."""
    import importlib.util
    import os
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.engine import make_url
    from app.migration_apply_gate import ephemeral_postgres_database

    database_url = os.getenv("DATABASE_URL", "")
    if os.getenv("USE_TEST_POSTGRES", "").lower() != "true" or not database_url.startswith(
        "postgresql"
    ):
        pytest.skip("requires the real PostgreSQL migration harness")

    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"

    def load(revision: str, filename: str):
        spec = importlib.util.spec_from_file_location(revision, versions / filename)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    migration_0755 = load("migration_0755", "0755_acceptance_criterion_contract_audit.py")
    migration_0756 = load(
        "migration_0756", "0756_release_authorship_reconciliation_state.py"
    )

    def run(migration, connection, operation):
        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            getattr(migration, operation)()
        finally:
            migration.op = original_op

    async with (
        ephemeral_postgres_database(make_url(database_url)) as (engine, _name),
        engine.begin() as connection,
    ):
        def seed_and_upgrade(sync_connection):
            sync_connection.exec_driver_sql(
                "CREATE TABLE versionacceptancecriterion ("
                "id uuid PRIMARY KEY, evidence_contract jsonb, "
                "created_at timestamptz NOT NULL, created_by varchar(64))"
            )
            run(migration_0755, sync_connection, "upgrade")
            sync_connection.exec_driver_sql(
                "INSERT INTO versionacceptancecriterion "
                "(id, evidence_contract, created_at, created_by) VALUES "
                "('00000000-0000-0000-0000-000000000001', '{\"predicate\":{}}', "
                "'2026-01-01T00:00:00+00:00', 'operator:legacy'), "
                "('00000000-0000-0000-0000-000000000002', '{\"predicate\":{}}', "
                "'2026-01-02T00:00:00+00:00', NULL)"
            )
            run(migration_0756, sync_connection, "upgrade")
            state = sync_connection.exec_driver_sql(
                "SELECT id, cursor_partition, generation "
                "FROM releaseauthorshipreconciliationstate"
            ).mappings().one()
            state_columns = {
                column["name"]: column
                for column in sa.inspect(sync_connection).get_columns(
                    "releaseauthorshipreconciliationstate"
                )
            }
            state_pk = sa.inspect(sync_connection).get_pk_constraint(
                "releaseauthorshipreconciliationstate"
            )
            audit = sync_connection.exec_driver_sql(
                "SELECT evidence_contract_updated_at, evidence_contract_updated_by "
                "FROM versionacceptancecriterion ORDER BY id"
            ).mappings().all()
            assert state == {"id": 1, "cursor_partition": 0, "generation": 0}
            assert state_pk["constrained_columns"] == ["id"]
            assert {
                "cursor_partition",
                "null_cursor_id",
                "served_at_cursor",
                "served_id_cursor",
                "generation",
            } <= state_columns.keys()
            assert audit[0]["evidence_contract_updated_by"] == "operator:legacy"
            assert audit[1]["evidence_contract_updated_by"] == "legacy:created_at"
            assert str(audit[0]["evidence_contract_updated_at"])[:10] == "2026-01-01"
            assert str(audit[1]["evidence_contract_updated_at"])[:10] == "2026-01-02"

            run(migration_0756, sync_connection, "downgrade")
            assert not sync_connection.dialect.has_table(
                sync_connection, "releaseauthorshipreconciliationstate"
            )
            run(migration_0756, sync_connection, "upgrade")
            run(migration_0756, sync_connection, "downgrade")
            run(migration_0755, sync_connection, "downgrade")
            columns = {
                row["column_name"]
                for row in sync_connection.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'versionacceptancecriterion'"
                ).mappings()
            }
            assert "evidence_contract_updated_by" not in columns
            assert "evidence_contract_updated_at" not in columns

        await connection.run_sync(seed_and_upgrade)
@pytest.mark.asyncio
async def test_reconciliation_stops_after_session_cleanup_failure(db_session, mocker):
    """A cleanup failure is terminal for the sweep, not a reason to try another row."""
    from app.database import AsyncSessionLocal
    from app.release_commit_authorship import _ReconciliationCleanupError

    project = Project(name="Authorship cleanup terminal", user_id="authorship-cleanup")
    rows = [
        ReleaseVersion(
            project_id=project.id,
            version_number=index + 1,
            status="promoted",
            base_sha=f"{index + 1:040x}",
            served_sha=f"{index + 2:040x}",
            serving_status="serving_confirmed",
            served_at=datetime(2026, 1, index + 1, tzinfo=timezone.utc),
            opened_at=datetime(2026, 1, index + 1, tzinfo=timezone.utc),
            last_qualification={},
        )
        for index in range(2)
    ]
    db_session.add_all([project, *rows])
    await db_session.commit()

    contexts_created = 0

    class FailingExitContext:
        def __init__(self, context, *, fail_cleanup):
            self.context = context
            self.fail_cleanup = fail_cleanup

        async def __aenter__(self):
            return await self.context.__aenter__()

        async def __aexit__(self, *exc_info):
            result = await self.context.__aexit__(*exc_info)
            if self.fail_cleanup:
                raise _ReconciliationCleanupError("connection cleanup failed")
            return result

    def session_factory():
        nonlocal contexts_created
        contexts_created += 1
        return FailingExitContext(
            AsyncSessionLocal(), fail_cleanup=contexts_created == 3
        )

    measure = mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(return_value=True),
    )

    attempted = await reconcile_serving_authorship_evidence(session_factory, limit=2)

    assert attempted == 0
    assert contexts_created == 3  # two scan partitions; no second worker after cleanup failure
    measure.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_keyset_scans_mixed_malformed_rows_without_json_casts(
    db_session, mocker
):
    """Postgres sweep walks past valid rows and repairs malformed JSON claims."""
    from app.database import AsyncSessionLocal

    project = Project(name="Authorship mixed rows", user_id="authorship-mixed")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    base, head = "a" * 40, "b" * 40

    def valid(state: str, next_retry_at: str | None = None) -> dict:
        return {
            "source": AUTHORSHIP_RECEIPT_SOURCE,
            "schema_version": AUTHORSHIP_RECEIPT_SCHEMA_VERSION,
            "state": state,
            "count": 0 if state == "resolved" else None,
            "offenders": [],
            "range": {"base_sha": base, "head_sha": head},
            **({"reason": "terminal"} if state != "resolved" else {}),
            "measured_at": "2026-01-01T00:00:00+00:00",
            "retry": {
                "attempt": 1,
                "retryable": state == "unresolved",
                "next_retry_at": next_retry_at,
            },
        }

    reports = [
        None,
        {"schema_version": 1, "state": "resolved"},
        {"schema_version": "2", "state": "resolved"},
        {"schema_version": [], "state": "resolved"},
        {"schema_version": {}, "state": "resolved"},
        {"schema_version": 2, "state": "resolved", "retry": {"next_retry_at": []}},
        {"schema_version": 2, "state": "terminal", "retry": {"next_retry_at": "2099"}},
        valid("resolved"),
        valid("terminal"),
        valid("unresolved", "2099-01-01T00:00:00+00:00"),
    ]
    rows = [
        ReleaseVersion(
            project_id=project.id,
            version_number=index + 1,
            status="promoted",
            base_sha=base,
            served_sha=head,
            serving_status="serving_confirmed",
            served_at=datetime(2026, 1, 1, index, tzinfo=timezone.utc),
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification=(
                {"operator_authored_commits": report} if report is not None else {}
            ),
        )
        for index, report in enumerate(reports)
    ]
    db_session.add_all(rows)
    await db_session.commit()

    mocker.patch("app.release_commit_authorship._RECONCILIATION_SCAN_PAGE_SIZE", 2)
    measure = mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(return_value=True),
    )

    attempted = await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=10)

    assert attempted == 7
    assert measure.await_count == 7
    # The last three rows were valid current receipts and stayed untouched;
    # malformed v2 resolved/terminal claims were included in the repair set.
    await db_session.refresh(rows[-1])
    assert rows[-1].last_qualification["operator_authored_commits"]["state"] == "unresolved"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="skip_locked contention requires the PostgreSQL harness",
)
async def test_reconciliation_skip_locked_moves_past_contended_candidate(db_session, mocker):
    """A held row is skipped and cannot consume the sweep's only candidate slot."""
    from app.database import AsyncSessionLocal
    from sqlalchemy import select

    project = Project(name="Authorship contention", user_id="authorship-contention")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    rows = [
        ReleaseVersion(
            project_id=project.id,
            version_number=index + 1,
            status="promoted",
            base_sha=f"{index + 1:040x}",
            served_sha=f"{index + 2:040x}",
            serving_status="serving_confirmed",
            served_at=datetime(2026, 1, 1, index, tzinfo=timezone.utc),
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_qualification={},
        )
        for index in range(2)
    ]
    db_session.add_all(rows)
    await db_session.commit()

    blocker = AsyncSessionLocal()
    await blocker.begin()
    await blocker.execute(
        select(ReleaseVersion)
        .where(ReleaseVersion.id == rows[0].id)
        .with_for_update()
    )
    measure = mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(return_value=True),
    )
    try:
        attempted = await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1)
    finally:
        await blocker.rollback()
        await blocker.close()

    assert attempted == 1
    assert measure.await_args.args[0] == rows[1].id


@pytest.mark.asyncio
async def test_reconciliation_deadline_cancels_slow_external_measurement(db_session, mocker):
    """The sweep deadline applies to external I/O, not only candidate SQL."""
    project = Project(name="Authorship deadline", user_id="authorship-deadline")
    row = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        base_sha="a" * 40,
        served_sha="b" * 40,
        serving_status="serving_confirmed",
        served_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_qualification={},
    )
    db_session.add_all([project, row])
    await db_session.commit()
    from app.database import AsyncSessionLocal

    async def slow_measure(*_args, **_kwargs):
        await asyncio.sleep(0.2)

    mocker.patch(
        "app.release_commit_authorship._RECONCILIATION_MAX_SECONDS", 0.05
    )
    mocker.patch(
        "app.release_commit_authorship.measure_serving_authorship_evidence",
        new=AsyncMock(side_effect=slow_measure),
    )
    started = asyncio.get_running_loop().time()
    attempted = await reconcile_serving_authorship_evidence(AsyncSessionLocal, limit=1)
    elapsed = asyncio.get_running_loop().time() - started

    assert attempted == 1
    assert elapsed < 0.15


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
    assert "operator_authored_commits" not in (version.last_qualification or {})
    measurement.assert_not_awaited()
