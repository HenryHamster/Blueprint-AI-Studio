"""Exact lifecycle and metadata reconciliation tests for existing open PRs."""

import pytest
from fastapi import HTTPException


class MockResponse:
    """Minimal stand-in for an httpx response used by the GitHub helpers."""

    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_create_pr_reconciles_existing_open_pr_when_opted_in(mocker):
    """The agent publication path refreshes normalized evidence on dedup."""
    from app import github_pulls

    head_sha = "a" * 40
    existing_pr = {
        "number": 9,
        "html_url": "https://github.com/owner/repo/pull/9",
        "state": "open",
        "head": {
            "ref": "feature-head",
            "sha": head_sha,
            "repo": {"full_name": "Owner/Repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "OWNER/repo"}},
        "title": "stale title",
        "body": "stale body",
    }
    updated_pr = {
        **existing_pr,
        "title": "T" * 256,
        "body": "B" * 65536,
    }
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, [existing_pr]),
                MockResponse(200, updated_pr),
            ]
        ),
    )

    result = await github_pulls.create_pull_request(
        "ghs_token",
        "owner",
        "repo",
        "T" * 300,
        "feature-head",
        "main",
        body="B" * 70000,
        reconcile_existing=True,
        expected_head_sha=head_sha,
    )

    assert result == updated_pr
    assert [call.args[0] for call in mock_request.call_args_list] == ["GET", "PATCH"]
    assert mock_request.call_args_list[1].kwargs["json"] == {
        "title": "T" * 256,
        "body": "B" * 65536,
    }
    assert "head" not in mock_request.call_args_list[1].kwargs["json"]
    assert "base" not in mock_request.call_args_list[1].kwargs["json"]


@pytest.mark.asyncio
async def test_create_pr_reconciles_existing_open_pr_after_422_race(mocker):
    """A create race converges on the exact existing PR and refreshes its body."""
    from app import github_pulls

    head_sha = "a" * 40
    existing_pr = {
        "number": 9,
        "html_url": "https://github.com/owner/repo/pull/9",
        "state": "open",
        "head": {
            "ref": "feature-head",
            "sha": head_sha,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        "title": "stale title",
        "body": "stale body",
    }
    updated_pr = {**existing_pr, "title": "fresh title", "body": "fresh body"}
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, []),
                MockResponse(
                    422,
                    {
                        "errors": [
                            {"message": "A pull request already exists for this head"}
                        ]
                    },
                ),
                MockResponse(200, [existing_pr]),
                MockResponse(200, updated_pr),
            ]
        ),
    )

    result = await github_pulls.create_pull_request(
        "ghs_token",
        "owner",
        "repo",
        "fresh title",
        "feature-head",
        "main",
        body="fresh body",
        reconcile_existing=True,
        expected_head_sha=head_sha,
    )

    assert result == updated_pr
    assert [call.args[0] for call in mock_request.call_args_list] == [
        "GET",
        "POST",
        "GET",
        "PATCH",
    ]


@pytest.mark.asyncio
async def test_create_pr_legacy_caller_dedups_after_422_without_patch(mocker):
    """A rolling-deploy old agent preserves legacy idempotency after a race."""
    from app import github_pulls

    existing_pr = {
        "number": 9,
        "html_url": "https://github.com/owner/repo/pull/9",
        "state": "open",
    }
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, []),
                MockResponse(
                    422,
                    {
                        "errors": [
                            {"message": "A pull request already exists for this head"}
                        ]
                    },
                ),
                MockResponse(200, [existing_pr]),
            ]
        ),
    )

    result = await github_pulls.create_pull_request(
        "ghs_token",
        "owner",
        "repo",
        "legacy title",
        "feature-head",
        "main",
        body="legacy body",
        reconcile_existing=False,
        expected_head_sha=None,
    )

    assert result == existing_pr
    assert [call.args[0] for call in mock_request.call_args_list] == [
        "GET",
        "POST",
        "GET",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["head_ref", "head_sha", "head_repo", "base_ref", "base_repo"],
)
async def test_create_pr_reconcile_identity_mismatch_fails_closed(mocker, mismatch):
    """A stale/misbound lookup result cannot be PATCHed by task publication."""
    from app import github_pulls

    head_sha = "a" * 40
    mismatched_pr = {
        "number": 9,
        "html_url": "https://github.com/owner/repo/pull/9",
        "state": "open",
        "head": {
            "ref": "feature-head",
            "sha": head_sha,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
    }
    if mismatch == "head_ref":
        mismatched_pr["head"]["ref"] = "someone-elses-head"
    elif mismatch == "head_sha":
        mismatched_pr["head"]["sha"] = "b" * 40
    elif mismatch == "head_repo":
        mismatched_pr["head"]["repo"]["full_name"] = "owner/other"
    elif mismatch == "base_ref":
        mismatched_pr["base"]["ref"] = "release"
    else:
        mismatched_pr["base"]["repo"]["full_name"] = "owner/other"
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, [mismatched_pr])),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token",
            "owner",
            "repo",
            "fresh title",
            "feature-head",
            "main",
            body="fresh body",
            reconcile_existing=True,
            expected_head_sha=head_sha,
        )

    assert exc_info.value.status_code == 409
    assert mock_request.call_count == 1
    assert mock_request.call_args.args[0] == "GET"


@pytest.mark.asyncio
async def test_create_pr_reconcile_refuses_ambiguous_open_matches(mocker):
    """Multiple candidates never choose one to mutate."""
    from app import github_pulls

    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(
                200,
                [
                    {"number": 9, "state": "open"},
                    {"number": 10, "state": "open"},
                ],
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token",
            "owner",
            "repo",
            "fresh title",
            "feature-head",
            "main",
            body="fresh body",
            reconcile_existing=True,
            expected_head_sha="a" * 40,
        )

    assert exc_info.value.status_code == 503
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_create_pr_reconcile_rejects_changed_lifecycle_after_patch(mocker):
    """A PATCH response that no longer names the exact lifecycle is retryable."""
    from app import github_pulls

    head_sha = "a" * 40
    existing_pr = {
        "number": 9,
        "state": "open",
        "head": {
            "ref": "feature-head",
            "sha": head_sha,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        "title": "stale title",
        "body": "stale body",
    }
    changed_pr = {
        **existing_pr,
        "state": "closed",
        "title": "fresh title",
        "body": "fresh body",
    }
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, [existing_pr]),
                MockResponse(200, changed_pr),
            ]
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token",
            "owner",
            "repo",
            "fresh title",
            "feature-head",
            "main",
            body="fresh body",
            reconcile_existing=True,
            expected_head_sha=head_sha,
        )

    assert exc_info.value.status_code == 503
    assert int((exc_info.value.headers or {})["Retry-After"]) > 0
    assert [call.args[0] for call in mock_request.call_args_list] == ["GET", "PATCH"]
@pytest.mark.asyncio
async def test_create_pr_reconcile_rejects_stale_metadata_after_patch(mocker):
    """A 200 that did not apply the fresh title/body is not reconciliation."""
    from app import github_pulls

    head_sha = "a" * 40
    existing_pr = {
        "number": 9,
        "state": "open",
        "head": {
            "ref": "feature-head",
            "sha": head_sha,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        "title": "stale title",
        "body": "stale body",
    }
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, [existing_pr]),
                MockResponse(200, existing_pr),
            ]
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token",
            "owner",
            "repo",
            "fresh title",
            "feature-head",
            "main",
            body="fresh body",
            reconcile_existing=True,
            expected_head_sha=head_sha,
        )

    assert exc_info.value.status_code == 503
    assert int((exc_info.value.headers or {})["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_create_pr_reconcile_patch_failure_is_retryable(mocker):
    """A failed refresh must not return a false-success stale PR URL."""
    from app import github_pulls

    head_sha = "a" * 40
    existing_pr = {
        "number": 9,
        "html_url": "https://github.com/owner/repo/pull/9",
        "state": "open",
        "head": {
            "ref": "feature-head",
            "sha": head_sha,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        "title": "stale title",
        "body": "stale body",
    }
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, [existing_pr]),
                MockResponse(500, {"message": "upstream failed"}),
            ]
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token",
            "owner",
            "repo",
            "fresh title",
            "feature-head",
            "main",
            body="fresh body",
            reconcile_existing=True,
            expected_head_sha=head_sha,
        )

    assert exc_info.value.status_code == 503
    assert int((exc_info.value.headers or {})["Retry-After"]) > 0
    assert [call.args[0] for call in mock_request.call_args_list] == ["GET", "PATCH"]
