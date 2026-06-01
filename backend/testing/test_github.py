import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


class MockResponse:
    """Reusable mock for httpx responses."""

    def __init__(self, status_code, json_data=None, text="", headers=None, links=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}
        self.links = links or {}

    def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_load_tasks_for_artifacts_batches_by_task_id():
    """Artifact task lookup is batched into one query and deduped by task_id."""
    from app.routers.github import _load_tasks_for_artifacts

    class _ArtifactStub:
        def __init__(self, task_id):
            self.task_id = task_id

    class _TaskStub:
        def __init__(self, task_id):
            self.id = task_id

    class _ScalarResultStub:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _ExecuteResultStub:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _ScalarResultStub(self._rows)

    class _SessionStub:
        def __init__(self, rows):
            self.rows = rows
            self.executions = 0

        async def execute(self, _stmt):
            self.executions += 1
            return _ExecuteResultStub(self.rows)

    session = _SessionStub([_TaskStub(1), _TaskStub(2)])
    artifacts = [_ArtifactStub(1), _ArtifactStub(2), _ArtifactStub(1)]

    task_map = await _load_tasks_for_artifacts(session, artifacts)

    assert session.executions == 1
    assert set(task_map) == {1, 2}
    assert task_map[1].id == 1
    assert task_map[2].id == 2


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_validate_fine_grained_pat_rejects_classic_token():
    """
    A classic token (ghp_) is immediately rejected with HTTP 400
    without hitting the GitHub API.
    """
    from app.github import validate_fine_grained_pat

    with pytest.raises(HTTPException) as exc_info:
        await validate_fine_grained_pat("ghp_classic_token_123")

    assert exc_info.value.status_code == 400
    assert "Invalid token format" in exc_info.value.detail


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_validate_fine_grained_pat_success(mocker):
    """
    A valid Fine-Grained PAT returns the authenticated user's login.
    """
    from app.github import validate_fine_grained_pat

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(200, {"login": "testuser"})

    result = await validate_fine_grained_pat("github_pat_valid_token_123")

    assert result["user"] == "testuser"
    assert result["status"] == "valid"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_validate_fine_grained_pat_unauthorized_raises_401(mocker):
    """
    An expired or revoked token raises HTTPException(401).
    """
    from app.github import validate_fine_grained_pat

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(401, {"message": "Bad credentials"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_fine_grained_pat("github_pat_expired_token_123")

    assert exc_info.value.status_code == 401
    assert "invalid or expired" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Integration tests: POST /api/github/pat endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_github_pat_invalid_format(client, mocker):
    """
    A classic token (ghp_) is rejected with HTTP 400 by the endpoint.
    """
    mocker.patch(
        "app.routers.github.validate_fine_grained_pat",
        side_effect=HTTPException(status_code=400, detail="Invalid token format"),
    )

    response = await client.post(
        "/api/github/pat", json={"pat": "ghp_classic_token_123"}
    )

    assert response.status_code == 400


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_post_github_pat_valid(client, mocker):
    """
    A valid fine-grained PAT returns HTTP 200 with status=success.
    """
    mocker.patch(
        "app.routers.github.validate_fine_grained_pat",
        return_value={"user": "testuser", "status": "valid"},
    )
    mocker.patch(
        "app.routers.github.store_user_secret",
        return_value="projects/test/secrets/github-pat-test-user-123",
    )

    response = await client.post(
        "/api/github/pat", json={"pat": "github_pat_valid_token_abc123"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.asyncio
async def test_post_github_pat_unauthorized(client, mocker):
    """
    An expired or revoked PAT returns HTTP 401 from the endpoint.
    """
    mocker.patch(
        "app.routers.github.validate_fine_grained_pat",
        side_effect=HTTPException(status_code=401, detail="Token invalid or expired"),
    )

    response = await client.post(
        "/api/github/pat", json={"pat": "github_pat_expired_token_xyz"}
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_post_github_pat_secret_manager_outage_returns_503(
    client, db_session, mocker
):
    """
    When Secret Manager is unavailable during PAT registration, the endpoint
    must return HTTP 503 and must NOT persist the raw PAT in the database.
    """
    from sqlmodel import select as sql_select
    from app.models import UserIntegration

    mocker.patch(
        "app.routers.github.validate_fine_grained_pat",
        return_value={"user": "testuser", "status": "valid"},
    )
    mocker.patch(
        "app.routers.github.store_user_secret",
        side_effect=Exception("Secret Manager connection refused"),
    )

    response = await client.post(
        "/api/github/pat",
        json={"pat": "github_pat_valid_token_abc123"},
    )

    assert response.status_code == 503
    # The app's HTTPException handler in app/main.py reshapes HTTPException
    # responses to {"error": {"code", "message"}} (not the FastAPI default
    # {"detail": ...}), so we assert on the wrapped shape.
    body = response.json()
    assert body["error"]["code"] == 503
    assert "unavailable" in body["error"]["message"].lower()

    # Confirm no UserIntegration row was written
    result = await db_session.execute(
        sql_select(UserIntegration).where(UserIntegration.user_id == "test-user-123")
    )
    assert result.scalars().first() is None


# ---------------------------------------------------------------------------
# Edge-case tests: merge_pull_request, get_check_runs, rate-limiting, expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pull_request_already_merged(mocker):
    """
    merge_pull_request() on an already-merged PR (GitHub 405) should return
    {merged: False} without raising an exception.
    """
    from app.github import merge_pull_request

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(
        405,
        {"message": "Pull Request is not mergeable"},
        text="Pull Request is not mergeable",
    )

    result = await merge_pull_request("token", "owner", "repo", 42)

    assert result["merged"] is False
    assert result.get("conflict") is False
    assert "message" in result


@pytest.mark.asyncio
async def test_merge_pull_request_conflict(mocker):
    """
    merge_pull_request() with a merge conflict (GitHub 409) should return
    {merged: False, conflict: True} without raising.
    """
    from app.github import merge_pull_request

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(
        409,
        {"message": "Merge conflict"},
        text="Merge conflict",
    )

    result = await merge_pull_request("token", "owner", "repo", 42)

    assert result["merged"] is False
    assert result["conflict"] is True
    assert "message" in result


@pytest.mark.asyncio
async def test_reopen_pull_request_success(mocker):
    """reopen_pull_request() returns True on a 200 response."""
    from app.github import reopen_pull_request

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(200, {"number": 42, "state": "open"})

    result = await reopen_pull_request("token", "owner", "repo", 42)

    assert result is True
    _, kwargs = mock_request.call_args
    assert kwargs.get("json") == {"state": "open"}


@pytest.mark.asyncio
async def test_reopen_pull_request_failure(mocker):
    """reopen_pull_request() returns False on a non-200 response."""
    from app.github import reopen_pull_request

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(
        422, {"message": "cannot reopen"}, text="cannot reopen"
    )

    result = await reopen_pull_request("token", "owner", "repo", 42)

    assert result is False


@pytest.mark.asyncio
async def test_add_issue_label_success(mocker):
    """add_issue_label() returns response JSON on a 200 response."""
    from app.github import add_issue_label

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(200, [{"name": "scope-overflow"}])

    result = await add_issue_label("token", "owner", "repo", 42, "scope-overflow")

    assert result == [{"name": "scope-overflow"}]
    _, kwargs = mock_request.call_args
    assert kwargs.get("json") == {"labels": ["scope-overflow"]}


@pytest.mark.asyncio
async def test_add_issue_label_failure_raises_http_exception(mocker):
    """add_issue_label() raises HTTPException(500) on non-200 response."""
    from app.github import add_issue_label

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(
        404, {"message": "Not Found"}, text="Not Found"
    )

    with pytest.raises(HTTPException) as exc_info:
        await add_issue_label("token", "owner", "repo", 42, "scope-overflow")

    assert exc_info.value.status_code == 500
    assert "Failed to apply issue label on GitHub." in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# enable_auto_merge — GraphQL enablePullRequestAutoMerge mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_auto_merge_success(mocker):
    """Happy path: REST fetch returns node_id, GraphQL mutation succeeds."""
    from app.github import enable_auto_merge

    responses = [
        # 1. get_pull_request — returns node_id
        MockResponse(200, {"node_id": "PR_kwABC123", "number": 42}),
        # 2. GraphQL mutation — returns enabledAt timestamp
        MockResponse(
            200,
            {
                "data": {
                    "enablePullRequestAutoMerge": {
                        "pullRequest": {
                            "autoMergeRequest": {"enabledAt": "2026-04-15T08:00:00Z"}
                        }
                    }
                }
            },
        ),
    ]

    async def mock_request(method, *args, **kwargs):
        return responses.pop(0)

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    result = await enable_auto_merge("token", "owner", "repo", 42)

    assert result["enabled"] is True
    assert "2026-04-15T08:00:00Z" in result["message"]


@pytest.mark.asyncio
async def test_enable_auto_merge_rejected_by_github(mocker):
    """GraphQL application-level error (e.g. dirty PR) → returns {enabled: False} without raising."""
    from app.github import enable_auto_merge

    responses = [
        MockResponse(200, {"node_id": "PR_kwABC123", "number": 42}),
        MockResponse(
            200,
            {
                "data": {"enablePullRequestAutoMerge": None},
                "errors": [{"message": "Pull request is in clean status"}],
            },
        ),
    ]

    async def mock_request(method, *args, **kwargs):
        return responses.pop(0)

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    result = await enable_auto_merge("token", "owner", "repo", 42)

    assert result["enabled"] is False
    assert "clean status" in result["message"]


@pytest.mark.asyncio
async def test_enable_auto_merge_graphql_http_failure(mocker):
    """Non-200 GraphQL response → returns {enabled: False} without raising.

    _github_request retries 5xx, so after retries exhaust it returns the last
    500 response and enable_auto_merge reports it without raising.
    """
    from app.github import enable_auto_merge

    call_count = 0

    async def mock_request(method, url, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if "graphql" in url:
            return MockResponse(500, {}, text="Internal server error")
        # REST get_pull_request — first call returns node_id
        return MockResponse(200, {"node_id": "PR_kwABC123", "number": 42})

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)
    mocker.patch("asyncio.sleep", return_value=None)  # skip retry backoff

    result = await enable_auto_merge("token", "owner", "repo", 42)

    assert result["enabled"] is False
    assert "500" in result["message"]


@pytest.mark.asyncio
async def test_enable_auto_merge_invalid_merge_method():
    """Invalid merge_method returns {enabled: False} immediately, no API call."""
    from app.github import enable_auto_merge

    result = await enable_auto_merge(
        "token", "owner", "repo", 42, merge_method="INVALID"
    )

    assert result["enabled"] is False
    assert "Invalid merge_method" in result["message"]


@pytest.mark.asyncio
async def test_get_check_runs_log_fetch_timeout(mocker):
    """
    get_check_runs() when the CI job-log request times out should fall back
    to annotations and return a valid result dict — must not raise.
    """
    from app.github import get_check_runs

    check_runs_payload = {
        "check_runs": [
            {
                "id": 123,
                "name": "Test Suite",
                "status": "completed",
                "conclusion": "failure",
                "output": {"summary": "Tests failed"},
            }
        ]
    }
    annotations_payload = [
        {"message": "assert False", "path": "test_foo.py", "start_line": 42}
    ]

    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Main check-runs listing
            return MockResponse(200, check_runs_payload)
        elif call_count == 2:
            # Job-log fetch — simulate network timeout
            raise Exception("Request timed out")
        else:
            # Annotations fallback
            return MockResponse(200, annotations_payload)

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    result = await get_check_runs("token", "owner", "repo", "abc123sha")

    assert result["status"] == "completed"
    assert result["conclusion"] == "failure"
    assert "failed_logs" in result
    # Fallback path should include some failure context
    assert result["failed_logs"] != ""


@pytest.mark.asyncio
async def test_get_check_runs_paginates_all_pages_failing_run_on_later_page(mocker):
    """
    get_check_runs() must fetch all pages of check-runs, not just the first page
    (GitHub's default page size is 30). A failing run on page 2 must be caught
    and aggregated correctly so the qualifier's CI gate doesn't false-green.
    """
    from app.github import get_check_runs

    # Page 1: 30 passing runs
    page_1_runs = [
        {
            "id": i,
            "name": f"test-{i}",
            "status": "completed",
            "conclusion": "success",
        }
        for i in range(30)
    ]
    page_1 = {"check_runs": page_1_runs}

    # Page 2: 15 runs, one of which fails
    page_2_runs = [
        {
            "id": 30 + i,
            "name": f"test-{30 + i}",
            "status": "completed",
            "conclusion": "failure" if i == 7 else "success",
        }
        for i in range(15)
    ]
    page_2 = {"check_runs": page_2_runs}

    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First page of check-runs
            return MockResponse(
                200,
                page_1,
                links={
                    "next": {
                        "url": "https://api.github.com/repos/owner/repo/commits/sha/check-runs?per_page=100&page=2"
                    }
                },
            )
        elif call_count == 2:
            # Second page of check-runs (has a failing run)
            return MockResponse(200, page_2, links={})
        else:
            return MockResponse(200, page_1)

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    result = await get_check_runs("token", "owner", "repo", "sha")

    # Must see 45 total runs (30 + 15)
    assert len(result["details"]) == 45
    # Aggregate conclusion must be "failure" because page 2 has a failing run
    assert result["conclusion"] == "failure"
    assert result["status"] == "completed"
    # Must have made at least 2 calls to fetch both pages
    assert call_count >= 2


@pytest.mark.asyncio
async def test_get_check_runs_paginates_all_pages_pending_run_on_later_page(mocker):
    """
    get_check_runs() must fetch all pages and correctly handle pending runs.
    A pending/in_progress run on page 2 must be caught so the aggregate status
    reflects that CI is still running, not completed.
    """
    from app.github import get_check_runs

    # Page 1: 25 completed passing runs
    page_1_runs = [
        {
            "id": i,
            "name": f"test-{i}",
            "status": "completed",
            "conclusion": "success",
        }
        for i in range(25)
    ]
    page_1 = {"check_runs": page_1_runs}

    # Page 2: 10 runs, one of which is still in_progress
    page_2_runs = [
        {
            "id": 25 + i,
            "name": f"test-{25 + i}",
            "status": "in_progress" if i == 3 else "completed",
            "conclusion": "success" if i != 3 else None,
        }
        for i in range(10)
    ]
    page_2 = {"check_runs": page_2_runs}

    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First page: all passing
            return MockResponse(
                200,
                page_1,
                links={
                    "next": {
                        "url": "https://api.github.com/repos/owner/repo/commits/sha/check-runs?per_page=100&page=2"
                    }
                },
            )
        elif call_count == 2:
            # Second page: has a pending run
            return MockResponse(200, page_2, links={})
        else:
            return MockResponse(200, page_1)

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    result = await get_check_runs("token", "owner", "repo", "sha")

    # Must see 35 total runs (25 + 10)
    assert len(result["details"]) == 35
    # Aggregate status must be "in_progress" because page 2 has a pending run
    assert result["status"] == "in_progress"
    # Conclusion is "success" because all concluded checks pass (pending check not concluded yet)
    assert result["conclusion"] == "success"


@pytest.mark.asyncio
async def test_merge_pull_request_rate_limit_retries_then_propagates(mocker):
    """
    A 429 rate-limit response should be retried (respecting Retry-After),
    and if all retries fail, raise an HTTPException.
    """
    from app.github import merge_pull_request

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(
        429,
        {"message": "API rate limit exceeded"},
        text="API rate limit exceeded",
        headers={"Retry-After": "1"},
    )
    mocker.patch("asyncio.sleep", return_value=None)

    with pytest.raises(HTTPException) as exc_info:
        await merge_pull_request("token", "owner", "repo", 42)

    assert exc_info.value.status_code >= 400
    # Should have retried (3 attempts total)
    assert mock_request.call_count == 3


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_validate_fine_grained_pat_expired_token_returns_401_not_500(mocker):
    """
    validate_fine_grained_pat() with an expired token (GitHub returns 401)
    should raise HTTPException(401), not HTTPException(500).
    """
    from app.github import validate_fine_grained_pat

    mock_request = mocker.patch("httpx.AsyncClient.request")
    mock_request.return_value = MockResponse(
        401,
        {"message": "Token has expired"},
        text="Token has expired",
    )

    with pytest.raises(HTTPException) as exc_info:
        await validate_fine_grained_pat("github_pat_expired_xyz_abc_123")

    assert exc_info.value.status_code == 401
    assert "invalid or expired" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 5xx retry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_request_mirrors_rate_limit_headers_into_shared_state(mocker):
    """Every _github_request response populates shared_state.github_quota from
    X-RateLimit-* headers so /api/health/detailed + dashboard widget can show
    live quota without each consumer re-parsing headers."""
    import app.shared_state as _ss
    from app.github import _github_request

    # Reset state so the test is independent
    _ss.github_quota.update(
        {"remaining": None, "limit": None, "reset_at": None, "last_updated": None}
    )

    async def mock_request(method, *args, **kwargs):
        return MockResponse(
            200,
            {"ok": True},
            headers={
                "X-RateLimit-Remaining": "3421",
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Reset": "1776400000",
            },
        )

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    # Install-token auth (ghs_ prefix) — only these responses update the
    # shared_state pill; App-JWT and unauthenticated responses live in
    # different rate-limit buckets and are intentionally filtered out.
    install_headers = {"Authorization": "Bearer ghs_fake_token"}
    resp = await _github_request("GET", "https://api.github.com/test", install_headers)
    assert resp.status_code == 200
    assert _ss.github_quota["remaining"] == 3421
    assert _ss.github_quota["limit"] == 5000
    assert _ss.github_quota["reset_at"] == 1776400000
    assert _ss.github_quota["last_updated"] is not None


@pytest.mark.asyncio
async def test_github_quota_keeps_lowest_remaining_within_reset_window(mocker):
    """Within the same reset window, the lowest observed `remaining` is kept
    so transient dips aren't hidden by a later higher value until the window
    actually rolls over."""
    import app.shared_state as _ss
    from app.github import _github_request

    _ss.github_quota.update(
        {"remaining": None, "limit": None, "reset_at": None, "last_updated": None}
    )

    responses = [
        MockResponse(
            200,
            {},
            headers={"X-RateLimit-Remaining": "500", "X-RateLimit-Reset": "1776400000"},
        ),
        MockResponse(
            200,
            {},
            headers={"X-RateLimit-Remaining": "50", "X-RateLimit-Reset": "1776400000"},
        ),
        MockResponse(
            200,
            {},
            headers={"X-RateLimit-Remaining": "120", "X-RateLimit-Reset": "1776400000"},
        ),
    ]
    it = iter(responses)

    async def mock_request(*args, **kwargs):
        return next(it)

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)
    install_headers = {"Authorization": "Bearer ghs_fake_token"}
    for _ in range(3):
        await _github_request("GET", "https://api.github.com/test", install_headers)
    # Lowest-observed within window wins.
    assert _ss.github_quota["remaining"] == 50

    # After the reset window rolls over, accept the fresh value.
    _ss.github_quota["reset_at"] = 1776400000  # sanity: still the old window

    async def fresh_window(*args, **kwargs):
        return MockResponse(
            200,
            {},
            headers={
                "X-RateLimit-Remaining": "4800",
                "X-RateLimit-Reset": "1776403600",
            },
        )

    mocker.patch("httpx.AsyncClient.request", side_effect=fresh_window)
    await _github_request("GET", "https://api.github.com/test", install_headers)
    assert _ss.github_quota["remaining"] == 4800
    assert _ss.github_quota["reset_at"] == 1776403600


@pytest.mark.asyncio
async def test_github_request_retries_on_502(mocker):
    """
    _github_request retries on 502 and succeeds on the second attempt.
    """
    from app.github import _github_request

    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockResponse(502, text="Bad Gateway")
        return MockResponse(200, {"ok": True})

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)
    mocker.patch("asyncio.sleep", return_value=None)

    resp = await _github_request("GET", "https://api.github.com/test", {})
    assert resp.status_code == 200
    assert call_count == 2


@pytest.mark.asyncio
async def test_github_request_returns_5xx_after_max_retries(mocker):
    """
    _github_request returns the 5xx response after exhausting retries.
    """
    from app.github import _github_request

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(503, text="Service Unavailable"),
    )
    mocker.patch("asyncio.sleep", return_value=None)

    resp = await _github_request(
        "GET", "https://api.github.com/test", {}, max_retries=3
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_github_request_no_retry_on_4xx(mocker):
    """
    _github_request does NOT retry on 4xx errors.
    """
    from app.github import _github_request

    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockResponse(404, text="Not Found")

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    resp = await _github_request("GET", "https://api.github.com/test", {})
    assert resp.status_code == 404
    assert call_count == 1


@pytest.mark.asyncio
async def test_github_request_retries_on_network_error(mocker):
    """
    _github_request retries on httpx network errors and succeeds on retry.
    """
    import httpx as httpx_mod
    from app.github import _github_request

    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx_mod.ConnectError("Connection refused")
        return MockResponse(200, {"ok": True})

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)
    mocker.patch("asyncio.sleep", return_value=None)

    resp = await _github_request("GET", "https://api.github.com/test", {})
    assert resp.status_code == 200
    assert call_count == 2


@pytest.mark.asyncio
async def test_github_request_raises_network_error_after_max_retries(mocker):
    """
    _github_request re-raises the network error after exhausting retries.
    """
    import httpx as httpx_mod
    from app.github import _github_request

    mocker.patch(
        "httpx.AsyncClient.request",
        side_effect=httpx_mod.ConnectError("Connection refused"),
    )
    mocker.patch("asyncio.sleep", return_value=None)

    with pytest.raises(httpx_mod.ConnectError):
        await _github_request("GET", "https://api.github.com/test", {}, max_retries=3)


@pytest.mark.asyncio
async def test_github_request_fails_fast_when_reset_is_far(mocker):
    """A 403 rate-limited response with a reset window further out than
    RATE_LIMIT_FAST_FAIL_THRESHOLD_S must return immediately instead of
    retrying three times.  Prevents the retry-amplification pattern where a
    single logical call gets counted as 3 in the per-endpoint quota counter
    while the bucket is empty anyway."""
    from app.github import _github_request
    import app.shared_state as _ss

    # Clear both buckets so the pre-call short-circuit doesn't fire.
    _ss.github_quota.update(
        {"remaining": None, "limit": None, "reset_at": None, "last_updated": None}
    )
    _ss.github_jwt_quota.update(
        {"remaining": None, "limit": None, "reset_at": None, "last_updated": None}
    )

    future_reset = int(time.time()) + 60  # 60s away, well past the 5s threshold
    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockResponse(
            403,
            {"message": "API rate limit exceeded"},
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(future_reset),
            },
        )

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)
    mock_sleep = mocker.patch("asyncio.sleep", return_value=None)

    resp = await _github_request(
        "GET",
        "https://api.github.com/test",
        {},
        max_retries=3,
    )
    assert resp.status_code == 403
    assert call_count == 1  # no retry
    assert mock_sleep.call_count == 0  # no sleep


@pytest.mark.asyncio
async def test_github_request_short_circuits_when_install_bucket_empty(mocker):
    """If shared_state says the install-token bucket is at 0 and reset is far,
    the request is never sent — a synthetic 403 is returned instead. Prevents
    burning the per-endpoint counter on calls we know will 403 anyway."""
    from app.github import _github_request
    import app.shared_state as _ss

    future_reset = int(time.time()) + 120
    _ss.github_quota.update(
        {
            "remaining": 0,
            "limit": 5000,
            "reset_at": future_reset,
            "last_updated": None,
        }
    )
    _ss.github_jwt_quota.update(
        {
            "remaining": None,
            "limit": None,
            "reset_at": None,
            "last_updated": None,
        }
    )

    mock_request = mocker.patch("httpx.AsyncClient.request")

    install_headers = {"Authorization": "Bearer ghs_fake_token"}
    resp = await _github_request(
        "GET",
        "https://api.github.com/test",
        install_headers,
    )
    assert resp.status_code == 403
    # Request was never sent to GitHub
    mock_request.assert_not_called()

    # Cleanup so subsequent tests don't inherit the short-circuit state
    _ss.github_quota.update({"remaining": None, "limit": None, "reset_at": None})


@pytest.mark.asyncio
async def test_github_request_short_circuits_when_jwt_bucket_empty(mocker):
    """Same short-circuit logic for App-JWT (/app/* endpoints). Without this,
    a drained JWT bucket would keep burning retries against access-token
    minting, cascade-stalling the entire pipeline."""
    from app.github import _github_request
    import app.shared_state as _ss

    future_reset = int(time.time()) + 120
    _ss.github_jwt_quota.update(
        {
            "remaining": 0,
            "limit": 60,
            "reset_at": future_reset,
            "last_updated": None,
        }
    )
    _ss.github_quota.update(
        {
            "remaining": None,
            "limit": None,
            "reset_at": None,
            "last_updated": None,
        }
    )

    mock_request = mocker.patch("httpx.AsyncClient.request")

    # URL containing /app/ triggers the JWT bucket classification.
    resp = await _github_request(
        "GET",
        "https://api.github.com/app/installations/12345",
        {},
    )
    assert resp.status_code == 403
    mock_request.assert_not_called()

    _ss.github_jwt_quota.update({"remaining": None, "limit": None, "reset_at": None})


# ---------------------------------------------------------------------------
# Cache wrapper tests: check_runs dedup, TTL, counter bump
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_check_runs_cached_dedupes_within_ttl(mocker):
    """Two calls for the same (owner, repo, head_sha) within TTL should only
    invoke the underlying get_check_runs once.  This is the scaling fix for
    rollup-with-N-children fan-out."""
    import app.shared_state as _ss
    import app.github as gh

    # Clear cache + counter to isolate this test
    gh._check_runs_cache.clear()
    _ss.github_cache_hits["check_runs"] = 0

    mock_inner = mocker.patch(
        "app.github.get_check_runs",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "success",
            "details": [],
            "failed_logs": "",
        },
    )

    r1 = await gh.get_check_runs_cached("tok", "owner", "repo", "cache_sha_1")
    r2 = await gh.get_check_runs_cached("tok", "owner", "repo", "cache_sha_1")
    r3 = await gh.get_check_runs_cached("tok", "owner", "repo", "cache_sha_1")

    assert r1 == r2 == r3
    assert mock_inner.call_count == 1  # deduped
    assert _ss.github_cache_hits["check_runs"] == 2  # two hits bump the counter


@pytest.mark.asyncio
async def test_get_check_runs_cached_expires_after_ttl(mocker):
    """A stale cache entry must not be served past its TTL — call the wrapper
    with ttl=0 to force an immediate miss."""
    import app.github as gh

    gh._check_runs_cache.clear()

    mock_inner = mocker.patch(
        "app.github.get_check_runs",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "success",
            "details": [],
            "failed_logs": "",
        },
    )

    await gh.get_check_runs_cached("tok", "owner", "repo", "cache_sha_ttl", ttl=0)
    await gh.get_check_runs_cached("tok", "owner", "repo", "cache_sha_ttl", ttl=0)

    assert mock_inner.call_count == 2  # ttl=0 ⇒ every call misses


@pytest.mark.asyncio
async def test_get_check_runs_cached_invalidate_clears_entry(mocker):
    """``invalidate_check_runs_cache`` drops a specific key so the next call
    hits GitHub instead of returning stale state."""
    import app.github as gh

    gh._check_runs_cache.clear()

    mock_inner = mocker.patch(
        "app.github.get_check_runs",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "success",
            "details": [],
            "failed_logs": "",
        },
    )

    await gh.get_check_runs_cached("tok", "o", "r", "cache_sha_inv")
    gh.invalidate_check_runs_cache("o", "r", "cache_sha_inv")
    await gh.get_check_runs_cached("tok", "o", "r", "cache_sha_inv")

    assert mock_inner.call_count == 2


@pytest.mark.asyncio
async def test_scan_pr_for_injection_cached_by_head_sha(mocker):
    """Two scans on the same PR head_sha should fetch PR data once (for the
    head_sha lookup) but skip the diff + commits fetches on the second call."""
    import app.shared_state as _ss
    import app.security as sec

    sec._scan_cache.clear()
    _ss.github_cache_hits["scan_pr"] = 0

    mock_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"head": {"sha": "scan_test_sha"}, "title": "x", "body": ""},
    )
    mock_diff = mocker.patch(
        "app.github.get_pull_request_diff",
        new_callable=AsyncMock,
        return_value="+ hello world\n",
    )
    mock_commits = mocker.patch(
        "app.github.get_pull_request_commits",
        new_callable=AsyncMock,
        return_value=[{"commit": {"message": "init"}}],
    )

    r1 = await sec.scan_pr_for_injection("tok", "o", "r", 1)
    r2 = await sec.scan_pr_for_injection("tok", "o", "r", 1)

    assert r1 == r2
    # First call hit all three. Second call hits PR (for head_sha) then returns cache.
    assert mock_pr.call_count == 2
    assert mock_diff.call_count == 1  # skipped on cache hit
    assert mock_commits.call_count == 1  # skipped on cache hit
    assert _ss.github_cache_hits["scan_pr"] == 1


@pytest.mark.asyncio
async def test_scan_pr_for_injection_no_head_sha_skips_cache(mocker):
    """If PR lookup fails, head_sha is None and we must not crash — the scan
    still runs (best effort) and nothing is cached."""
    import app.security as sec

    sec._scan_cache.clear()

    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=Exception("transient"),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new_callable=AsyncMock,
        return_value="",
    )
    mocker.patch(
        "app.github.get_pull_request_commits",
        new_callable=AsyncMock,
        return_value=[],
    )

    result = await sec.scan_pr_for_injection("tok", "o", "r", 2)
    assert result["blocked"] is False
    assert len(sec._scan_cache) == 0


@pytest.mark.asyncio
async def test_pr_merge_negative_cache_skips_refetch(mocker):
    """_verify_pr_merged called twice within the negative-TTL should only
    fetch the PR once — the second call returns False from the cache."""
    import uuid as _uuid
    from unittest.mock import AsyncMock as _AM, MagicMock
    import app.shared_state as _ss
    from app import rollup_management as rm

    rm._pr_merge_negative_cache.clear()
    _ss.github_cache_hits["pr_merge_negative"] = 0

    # Build a task-shaped object the function is willing to read.  Use a real
    # UUID so the cache keys compare correctly.
    task = MagicMock()
    task.id = _uuid.uuid4()
    task.project_id = _uuid.uuid4()
    task.context = {}

    # Session returns a PR artifact so the function proceeds to the API call.
    fake_artifact = MagicMock()
    fake_artifact.pr_url = "https://github.com/o/r/pull/42"

    class _ArtifactResult:
        def scalars(self):
            class _S:
                def first(self_inner):
                    return fake_artifact

            return _S()

    session = MagicMock()
    session.execute = _AM(return_value=_ArtifactResult())

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=_AM,
        return_value=("tok", "o", "r"),
    )
    mock_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=_AM,
        return_value={"merged": False, "state": "open"},
    )
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert",
        new_callable=_AM,
        return_value=None,
    )

    # First call: fetches PR, sees unmerged, stamps the negative cache.
    r1 = await rm._verify_pr_merged(task, session)
    # Second call within TTL: should NOT refetch.
    r2 = await rm._verify_pr_merged(task, session)

    assert r1 is False
    assert r2 is False
    assert mock_pr.call_count == 1
    assert _ss.github_cache_hits["pr_merge_negative"] == 1


@pytest.mark.asyncio
async def test_github_request_resets_client_on_connect_error(mocker):
    """
    On transport-layer failure (ConnectError/PoolTimeout/ReadError), the shared
    httpx client is torn down so the retry rebuilds TCP+TLS instead of reusing
    a potentially-dead connection from the pool. Without this, a single bad
    keep-alive can silently blackhole the pool for its entire lifetime.
    """
    import httpx as httpx_mod
    import app.github as gh

    # Prime the module's shared client so we have something to reset.
    gh._github_client = None
    first_client = gh._get_client()
    assert first_client is not None

    call_count = 0

    async def mock_request(method, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx_mod.ConnectError("Connection refused")
        return MockResponse(200, {"ok": True})

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)
    mocker.patch("asyncio.sleep", return_value=None)

    resp = await gh._github_request("GET", "https://api.github.com/test", {})
    assert resp.status_code == 200
    assert call_count == 2
    # Client was reset after the first failure — next call built a new instance.
    assert gh._github_client is not first_client


# ---------------------------------------------------------------------------
# Webhook tests: PR closed-without-merge sets pr_rejected on matching task
# ---------------------------------------------------------------------------

_TEST_WEBHOOK_SECRET = "test-webhook-secret-for-pytest"


def _make_webhook_sig(payload_bytes: bytes) -> str:
    """Compute the x-hub-signature-256 header value for a given payload."""
    return (
        "sha256="
        + hmac.new(
            _TEST_WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256
        ).hexdigest()
    )


def _pr_close_payload(
    owner: str, repo: str, pr_number: int, *, merged: bool, body: str = ""
) -> bytes:
    """Build a minimal pull_request closed webhook payload."""
    return json.dumps(
        {
            "action": "closed",
            "pull_request": {
                "number": pr_number,
                "merged": merged,
                "body": body,
            },
            "repository": {
                "full_name": f"{owner}/{repo}",
            },
        }
    ).encode()


@pytest.mark.asyncio
async def test_pr_close_without_merge_sets_pr_rejected(client, db_session, mocker):
    """
    A pull_request webhook with action=closed and merged=false marks the
    matching task as pr_rejected=True.
    """
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    # Prevent Redis dedup from being called (no delivery-id header used)

    project = Project(
        name="Rejection Test",
        user_id="user1",
        github_owner="testorg",
        github_repo="testrepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Fix bug",
        payloads=[{"type": "create", "prompt": "fix it"}],
        status=TaskStatus.COMPLETED,
        context={},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/testorg/testrepo/pull/42",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _pr_close_payload(
        "testorg", "testrepo", 42, merged=False, body="Not a good fit"
    )
    resp = await client.post(
        "/api/github/webhook",
        content=payload_bytes,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": f"test-{uuid.uuid4()}",
            "x-hub-signature-256": _make_webhook_sig(payload_bytes),
        },
    )

    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.context.get("pr_rejected") is True
    assert task.context.get("pr_rejection_note") == "Not a good fit"


@pytest.mark.asyncio
async def test_pr_close_without_merge_is_idempotent(client, db_session, mocker):
    """
    A second close-without-merge webhook for a task that is already
    pr_rejected=True does NOT overwrite existing context values.
    """
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    project = Project(
        name="Idempotent Test",
        user_id="user1",
        github_owner="testorg",
        github_repo="idempotentrepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Already rejected",
        payloads=[{"type": "create", "prompt": "fix it"}],
        status=TaskStatus.COMPLETED,
        context={"pr_rejected": True, "pr_rejection_note": "original note"},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/testorg/idempotentrepo/pull/7",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _pr_close_payload(
        "testorg", "idempotentrepo", 7, merged=False, body="second close"
    )
    resp = await client.post(
        "/api/github/webhook",
        content=payload_bytes,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": f"test-{uuid.uuid4()}",
            "x-hub-signature-256": _make_webhook_sig(payload_bytes),
        },
    )

    assert resp.status_code == 200

    await db_session.refresh(task)
    # Already-rejected task must not be re-processed
    assert task.context.get("pr_rejection_note") == "original note"


@pytest.mark.asyncio
async def test_pr_close_without_merge_unmatched_pr_is_noop(client, db_session, mocker):
    """
    A close-without-merge webhook for a PR number that has no matching
    artifact is a no-op — no tasks are modified.
    """
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    project = Project(
        name="Noop Test",
        user_id="user1",
        github_owner="testorg",
        github_repo="nooprepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Unrelated task",
        payloads=[{"type": "create", "prompt": "something"}],
        status=TaskStatus.COMPLETED,
        context={},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Artifact references PR #10; webhook is for PR #99
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/testorg/nooprepo/pull/10",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _pr_close_payload("testorg", "nooprepo", 99, merged=False)
    resp = await client.post(
        "/api/github/webhook",
        content=payload_bytes,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": f"test-{uuid.uuid4()}",
            "x-hub-signature-256": _make_webhook_sig(payload_bytes),
        },
    )

    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.context.get("pr_rejected") is None


# ---------------------------------------------------------------------------
# Webhook helpers shared by the three new handler test sections
# ---------------------------------------------------------------------------


def _make_pr_merged_payload(
    owner: str, repo: str, pr_number: int, body: str = ""
) -> bytes:
    """Build a minimal pull_request closed+merged webhook payload."""
    return json.dumps(
        {
            "action": "closed",
            "pull_request": {
                "number": pr_number,
                "merged": True,
                "body": body,
            },
            "repository": {
                "full_name": f"{owner}/{repo}",
            },
        }
    ).encode()


def _make_check_run_payload(
    owner: str,
    repo: str,
    head_sha: str,
    *,
    action: str = "completed",
    conclusion: str = "success",
    head_branch: str = "feature/test",
    pr_numbers: list[int] | None = None,
) -> bytes:
    """Build a minimal check_run webhook payload.

    ``pr_numbers`` populates ``check_run.pull_requests`` so the webhook
    handler's pr_number match path (for rollup parents with no head_sha)
    can be exercised.
    """
    _prs = [{"number": n} for n in (pr_numbers or [])]
    return json.dumps(
        {
            "action": action,
            "check_run": {
                "id": 999,
                "name": "CI / test",
                "status": "completed",
                "conclusion": conclusion,
                "head_sha": head_sha,
                "pull_requests": _prs,
                "check_suite": {
                    "head_branch": head_branch,
                    "pull_requests": [],
                },
            },
            "repository": {
                "full_name": f"{owner}/{repo}",
            },
        }
    ).encode()


def _make_pr_event_payload(
    owner: str,
    repo: str,
    pr_number: int,
    action: str,
    *,
    branch: str = "feature/my-branch",
    base_branch: str = "main",
    title: str = "My PR",
) -> bytes:
    """Build a minimal pull_request opened/synchronize webhook payload."""
    return json.dumps(
        {
            "action": action,
            "pull_request": {
                "number": pr_number,
                "merged": False,
                "body": "",
                "head": {"ref": branch},
                "base": {"ref": base_branch},
                "title": title,
            },
            "repository": {
                "full_name": f"{owner}/{repo}",
            },
        }
    ).encode()


async def _post_webhook(client, payload_bytes: bytes, event_type: str):
    """Send a signed webhook POST and return the response."""
    return await client.post(
        "/api/github/webhook",
        content=payload_bytes,
        headers={
            "content-type": "application/json",
            "x-github-event": event_type,
            "x-github-delivery": f"test-{uuid.uuid4()}",
            "x-hub-signature-256": _make_webhook_sig(payload_bytes),
        },
    )


# ---------------------------------------------------------------------------
# Webhook tests: pull_request closed+merged handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_merged_sets_pr_merged_on_matching_task(client, db_session, mocker):
    """
    A pull_request webhook with action=closed and merged=True sets pr_merged=True
    on the matching task's context.
    """
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    mocker.patch(
        "app.autonomous_research._check_autonomous_research", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Merge Test",
        user_id="user1",
        github_owner="mergeorg",
        github_repo="mergerepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Add feature",
        payloads=[{"type": "create", "prompt": "do it"}],
        status=TaskStatus.COMPLETED,
        context={},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/mergeorg/mergerepo/pull/55",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _make_pr_merged_payload("mergeorg", "mergerepo", 55)
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.context.get("pr_merged") is True


@pytest.mark.asyncio
async def test_pr_merged_dispatches_enriched_payload(client, db_session, mocker):
    """PR_MERGED webhook payload includes direction_text, scope, direction_id, changed_files."""
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    mocker.patch(
        "app.autonomous_research._check_autonomous_research", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Enriched Merge",
        user_id="user1",
        github_owner="enrichorg",
        github_repo="enrichrepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Add feature",
        payloads=[{"type": "create", "prompt": "do it"}],
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": "d-abc123",
            "direction_text": "Improve webhook payload richness",
            "scope": "small",
            "_pr_changed_files": ["a.py", "b.py", "c.py"],
        },
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/enrichorg/enrichrepo/pull/77",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _make_pr_merged_payload("enrichorg", "enrichrepo", 77)
    resp = await _post_webhook(client, payload_bytes, "pull_request")
    assert resp.status_code == 200

    # ensure_future schedules the coroutine — let event loop run it
    import asyncio as _asyncio

    await _asyncio.sleep(0)

    assert dispatch_mock.await_count >= 1
    args, _kwargs = (
        dispatch_mock.await_args_list[0].args,
        dispatch_mock.await_args_list[0].kwargs,
    )
    assert args[1] == "PR_MERGED"
    body = args[2]
    assert body["direction_id"] == "d-abc123"
    assert body["direction_text"] == "Improve webhook payload richness"
    assert body["scope"] == "small"
    assert body["changed_files"] == 3
    assert body["repo"] == "enrichorg/enrichrepo"


@pytest.mark.asyncio
async def test_pr_merged_unregistered_repo_ignored(client, db_session, mocker):
    """
    A closed+merged webhook for a repo that has no registered project returns
    status=ignored and makes no DB changes.
    """
    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    payload_bytes = _make_pr_merged_payload("ghostorg", "ghostrepo", 1)
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_pr_merged_already_merged_task_is_idempotent(client, db_session, mocker):
    """
    A second closed+merged webhook for a task that is already pr_merged=True
    does not trigger another rollup check.
    """
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    mocker.patch(
        "app.autonomous_research._check_autonomous_research", new_callable=AsyncMock
    )
    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Idempotent Merge",
        user_id="user1",
        github_owner="idemorg",
        github_repo="idemrepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Already merged",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/idemorg/idemrepo/pull/3",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _make_pr_merged_payload("idemorg", "idemrepo", 3)
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    # Task was already merged — rollup should NOT be triggered again
    rollup_mock.assert_not_called()


@pytest.mark.asyncio
async def test_pr_merged_triggers_rollup_for_affected_tasks(client, db_session, mocker):
    """
    A closed+merged webhook triggers _check_and_trigger_rollup for each
    task whose artifact matched the merged PR.
    """
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    mocker.patch(
        "app.autonomous_research._check_autonomous_research", new_callable=AsyncMock
    )
    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Rollup Test",
        user_id="user1",
        github_owner="rolluporg",
        github_repo="rolluprepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Rollup candidate",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context={},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/rolluporg/rolluprepo/pull/77",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _make_pr_merged_payload("rolluporg", "rolluprepo", 77)
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    rollup_mock.assert_called_once()
    called_task = rollup_mock.call_args[0][0]
    assert called_task.id == task.id


@pytest.mark.asyncio
async def test_pr_merged_no_matching_artifact_is_noop(client, db_session, mocker):
    """
    A closed+merged webhook for a PR number that has no matching artifact
    does not set pr_merged on any task.
    """
    from app.models import Artifact, ArtifactType, Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    mocker.patch(
        "app.autonomous_research._check_autonomous_research", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Noop Merge",
        user_id="user1",
        github_owner="noopmergeorg",
        github_repo="nooprepomerge",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Unrelated task",
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        context={},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Artifact for PR #10; webhook is for PR #99
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/noopmergeorg/nooprepomerge/pull/10",
    )
    db_session.add(artifact)
    await db_session.commit()

    payload_bytes = _make_pr_merged_payload("noopmergeorg", "nooprepomerge", 99)
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    await db_session.refresh(task)
    assert task.context.get("pr_merged") is None


# ---------------------------------------------------------------------------
# Webhook tests: check_run completed handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_run_non_completed_action_is_ignored(client, db_session, mocker):
    """
    A check_run webhook with action != 'completed' is immediately ignored.
    """
    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    payload_bytes = _make_check_run_payload(
        "ciorg", "cirepo", "abc123", action="created"
    )
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_check_run_unregistered_repo_ignored(client, db_session, mocker):
    """
    A completed check_run for an unregistered repo is ignored.
    """
    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    payload_bytes = _make_check_run_payload("ghostorg", "ghostrepo", "deadbeef")
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_check_run_completed_calls_handle_ci_completion(
    client, db_session, mocker
):
    """
    A completed check_run whose head_sha matches an EVALUATING ci_waiting task
    calls handle_ci_completion with the task.
    """
    from app.models import Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    head_sha = "cafebabe1234567890abcdef"
    gh_token = "gh-test-token"

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=(gh_token, "ciorg2", "cirepo2"),
    )
    mock_get_check_runs = mocker.patch(
        "app.github.get_check_runs",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
        },
    )
    mock_handle_ci = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new_callable=AsyncMock,
    )

    project = Project(
        name="CI Test",
        user_id="user1",
        github_owner="ciorg2",
        github_repo="cirepo2",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Waiting for CI",
        payloads=[{"type": "review", "pr_number": 10, "branch": "feature/x"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "head_sha": head_sha,
                "iteration": 1,
            }
        },
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()

    payload_bytes = _make_check_run_payload(
        "ciorg2", "cirepo2", head_sha, conclusion="success"
    )
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    mock_get_check_runs.assert_called_once()
    mock_handle_ci.assert_called_once()
    called_task = mock_handle_ci.call_args[0][0]
    assert called_task.id == task.id


@pytest.mark.asyncio
async def test_check_run_completed_matches_rollup_parent_by_pr_number(
    client, db_session, mocker
):
    """Rollup parents are set ci_waiting=True + pr_number but NOT head_sha
    (rollup_management.py). A check_run webhook that doesn't match head_sha
    must still route to handle_ci_completion via pr_number, otherwise the
    webhook is a no-op and polling burns GitHub API quota."""
    from app.models import Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    head_sha = "rolluphead123456789abcdef"
    pr_number = 4242
    gh_token = "gh-test-token"

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=(gh_token, "rollorg", "rollrepo"),
    )
    mocker.patch(
        "app.github.get_check_runs",
        new_callable=AsyncMock,
        return_value={
            "status": "completed",
            "conclusion": "success",
            "failed_logs": "",
        },
    )
    mock_handle_ci = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Rollup Webhook",
        user_id="user1",
        github_owner="rollorg",
        github_repo="rollrepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent: ci_waiting + pr_number, but head_sha intentionally absent.
    rollup_parent = Task(
        project_id=project.id,
        title="Rollup parent",
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "ci_waiting": True,
                "pr_number": pr_number,
                "branch": "research/abcd",
                "base_branch": "main",
                # NO head_sha — this is exactly the bug case
            }
        },
        created_by="user1",
    )
    db_session.add(rollup_parent)
    await db_session.commit()
    await db_session.refresh(rollup_parent)

    payload_bytes = _make_check_run_payload(
        "rollorg", "rollrepo", head_sha, conclusion="success", pr_numbers=[pr_number]
    )
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    # The critical assertion: webhook routed to handle_ci_completion despite
    # the head_sha mismatch, because pr_number matched.
    mock_handle_ci.assert_called_once()
    called_task = mock_handle_ci.call_args[0][0]
    assert called_task.id == rollup_parent.id
    # Backfill: head_sha is set on the in-session object so future
    # webhooks can use the fast-path match.  handle_ci_completion commits
    # after we add it in-session; the mock here doesn't commit, but the
    # attribute must be populated by the matching loop before it ran.
    assert (called_task.context or {}).get("review_loop", {}).get(
        "head_sha"
    ) == head_sha


@pytest.mark.asyncio
async def test_check_run_completed_pending_checks_skips_ci_completion(
    client, db_session, mocker
):
    """
    When get_check_runs returns status != 'completed' (some checks still pending),
    handle_ci_completion is NOT called.
    """
    from app.models import Project, Task, TaskStatus

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    head_sha = "pending1234567890abcdef"
    gh_token = "gh-test-token"

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=(gh_token, "pendorg", "pendrepo"),
    )
    mocker.patch(
        "app.github.get_check_runs",
        new_callable=AsyncMock,
        return_value={"status": "in_progress", "conclusion": None, "failed_logs": ""},
    )
    mock_handle_ci = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Pending CI",
        user_id="user1",
        github_owner="pendorg",
        github_repo="pendrepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Still waiting",
        payloads=[{"type": "review", "pr_number": 5, "branch": "feature/y"}],
        status=TaskStatus.EVALUATING,
        context={"review_loop": {"ci_waiting": True, "head_sha": head_sha}},
        created_by="user1",
    )
    db_session.add(task)
    await db_session.commit()

    payload_bytes = _make_check_run_payload("pendorg", "pendrepo", head_sha)
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    mock_handle_ci.assert_not_called()


@pytest.mark.asyncio
async def test_check_run_no_matching_tasks_returns_no_match(client, db_session, mocker):
    """
    When no EVALUATING tasks match the head_sha and auto-remediation is not
    configured, the handler returns status=no_match.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    project = Project(
        name="No Match",
        user_id="user1",
        github_owner="nomatchorg",
        github_repo="nomatchrepo",
    )
    db_session.add(project)
    await db_session.commit()

    payload_bytes = _make_check_run_payload(
        "nomatchorg", "nomatchrepo", "00000000", conclusion="success"
    )
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    assert resp.json()["status"] == "no_match"


@pytest.mark.asyncio
async def test_check_run_failure_triggers_auto_remediation(client, db_session, mocker):
    """
    A CI failure on a protected branch triggers _handle_main_branch_ci_failure
    when auto-remediation is enabled and the branch is whitelisted.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    remediation_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Auto Remediation",
        user_id="user1",
        github_owner="remediateorg",
        github_repo="remediaterepo",
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_whitelist": ["main"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()

    payload_bytes = _make_check_run_payload(
        "remediateorg",
        "remediaterepo",
        "failsha123456",
        conclusion="failure",
        head_branch="main",
    )
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    assert resp.json()["status"] == "auto_remediation_triggered"
    remediation_mock.assert_called_once()
    call_kwargs = remediation_mock.call_args[1]
    assert call_kwargs["head_branch"] == "main"
    assert call_kwargs["head_sha"] == "failsha123456"


@pytest.mark.asyncio
async def test_check_run_failure_auto_remediation_disabled(client, db_session, mocker):
    """
    A CI failure on a protected branch does NOT trigger auto-remediation when
    auto-remediation is disabled in the project config.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    project = Project(
        name="No Auto Remediation",
        user_id="user1",
        github_owner="nodoreorg",
        github_repo="nodorepo",
        design_doc={"_auto_remediation": {"enabled": False}},
    )
    db_session.add(project)
    await db_session.commit()

    payload_bytes = _make_check_run_payload(
        "nodoreorg", "nodorepo", "failsha999", conclusion="failure", head_branch="main"
    )
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored_auto_remediation_disabled"


@pytest.mark.asyncio
async def test_check_run_failure_branch_not_in_whitelist(client, db_session, mocker):
    """
    A CI failure on a branch NOT in the whitelist does not trigger auto-remediation.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)

    project = Project(
        name="Branch Not Whitelisted",
        user_id="user1",
        github_owner="wlorg",
        github_repo="wlrepo",
        design_doc={
            "_auto_remediation": {
                "enabled": True,
                "branch_whitelist": ["main"],
            }
        },
    )
    db_session.add(project)
    await db_session.commit()

    payload_bytes = _make_check_run_payload(
        "wlorg", "wlrepo", "devsha999", conclusion="failure", head_branch="develop"
    )
    resp = await _post_webhook(client, payload_bytes, "check_run")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored_auto_remediation_disabled"


# ---------------------------------------------------------------------------
# Webhook tests: pull_request opened/synchronize auto-review handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_opened_auto_review_enabled_triggers_review(
    client, db_session, mocker
):
    """
    A pull_request opened webhook triggers _handle_auto_review when the
    project has auto_review enabled.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    auto_review_mock = mocker.patch(
        "app.auto_review._handle_auto_review",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Auto Review",
        user_id="user1",
        github_owner="arorg",
        github_repo="arrepo",
        design_doc={"_auto_review": {"enabled": True}},
    )
    db_session.add(project)
    await db_session.commit()

    payload_bytes = _make_pr_event_payload("arorg", "arrepo", 42, "opened")
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    assert resp.json()["status"] == "auto_review_triggered"
    auto_review_mock.assert_called_once()
    call_kwargs = auto_review_mock.call_args[1]
    assert call_kwargs["pr_number"] == 42
    assert call_kwargs["owner"] == "arorg"


@pytest.mark.asyncio
async def test_pr_opened_auto_review_base_branch_fallback_uses_integration_branch(
    client, db_session, mocker
):
    """
    When the PR payload has an empty base.ref, the auto-review handler should
    receive base_branch from the project's configured integration_branch, not 'main'.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    auto_review_mock = mocker.patch(
        "app.auto_review._handle_auto_review",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Custom Branch Auto Review",
        user_id="user1",
        github_owner="customorg",
        github_repo="customrepo",
        integration_branch="automain",
        design_doc={"_auto_review": {"enabled": True}},
    )
    db_session.add(project)
    await db_session.commit()

    # Pass an empty string for base_branch to simulate a missing/empty base.ref
    payload_bytes = _make_pr_event_payload(
        "customorg", "customrepo", 99, "opened", base_branch=""
    )
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    assert resp.json()["status"] == "auto_review_triggered"
    auto_review_mock.assert_called_once()
    call_kwargs = auto_review_mock.call_args[1]
    assert call_kwargs["base_branch"] == "automain"


@pytest.mark.asyncio
async def test_pr_synchronize_auto_review_enabled_triggers_review(
    client, db_session, mocker
):
    """
    A pull_request synchronize webhook (push to existing PR) also triggers
    _handle_auto_review when auto_review is enabled.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    auto_review_mock = mocker.patch(
        "app.auto_review._handle_auto_review",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Auto Review Sync",
        user_id="user1",
        github_owner="arsyncorg",
        github_repo="arsyncrepo",
        design_doc={"_auto_review": {"enabled": True}},
    )
    db_session.add(project)
    await db_session.commit()

    payload_bytes = _make_pr_event_payload("arsyncorg", "arsyncrepo", 7, "synchronize")
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    assert resp.json()["status"] == "auto_review_triggered"
    auto_review_mock.assert_called_once()


@pytest.mark.asyncio
async def test_pr_opened_auto_review_disabled_no_trigger(client, db_session, mocker):
    """
    A pull_request opened webhook does NOT trigger auto_review when the
    project has auto_review disabled.
    """
    from app.models import Project

    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    auto_review_mock = mocker.patch(
        "app.auto_review._handle_auto_review",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Auto Review Disabled",
        user_id="user1",
        github_owner="ardisabledorg",
        github_repo="ardisabledrepo",
        design_doc={"_auto_review": {"enabled": False}},
    )
    db_session.add(project)
    await db_session.commit()

    payload_bytes = _make_pr_event_payload(
        "ardisabledorg", "ardisabledrepo", 5, "opened"
    )
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    # auto_review_triggered is NOT returned when disabled
    assert resp.json().get("status") != "auto_review_triggered"
    auto_review_mock.assert_not_called()


@pytest.mark.asyncio
async def test_pr_opened_unregistered_repo_no_auto_review(client, db_session, mocker):
    """
    A pull_request opened webhook for an unregistered repo does not trigger
    auto_review.
    """
    mocker.patch("app.routers.github.GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)
    auto_review_mock = mocker.patch(
        "app.auto_review._handle_auto_review",
        new_callable=AsyncMock,
    )

    payload_bytes = _make_pr_event_payload("unknownorg", "unknownrepo", 1, "opened")
    resp = await _post_webhook(client, payload_bytes, "pull_request")

    assert resp.status_code == 200
    auto_review_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_branch_with_sha_retry_success_first_attempt(mocker):
    """First-attempt success returns without refetching the PR."""
    from app import github as gh

    update_branch_mock = mocker.patch.object(
        gh,
        "update_branch",
        new_callable=AsyncMock,
        return_value={"updated": True, "message": "Update queued"},
    )
    get_pr_mock = mocker.patch.object(gh, "get_pull_request", new_callable=AsyncMock)

    result = await gh.update_branch_with_sha_retry(
        "t",
        "o",
        "r",
        1,
        expected_head_sha="abc",
    )

    assert result == {"updated": True, "message": "Update queued"}
    assert update_branch_mock.await_count == 1
    get_pr_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_branch_with_sha_retry_sha_mismatch_retry_succeeds(mocker):
    """Non-success + fresh SHA differs → retry with fresh SHA succeeds."""
    from app import github as gh

    update_branch_mock = mocker.patch.object(
        gh,
        "update_branch",
        new_callable=AsyncMock,
        side_effect=[
            {"updated": False, "message": "Some error wording GitHub might change"},
            {"updated": True, "message": "Update queued"},
        ],
    )
    mocker.patch.object(
        gh,
        "get_pull_request",
        new_callable=AsyncMock,
        return_value={"head": {"sha": "fresh123"}},
    )

    result = await gh.update_branch_with_sha_retry(
        "t",
        "o",
        "r",
        1,
        expected_head_sha="stale456",
    )

    assert result == {"updated": True, "message": "Update queued"}
    assert update_branch_mock.await_count == 2
    second_call_kwargs = update_branch_mock.await_args_list[1].kwargs
    assert second_call_kwargs["expected_head_sha"] == "fresh123"


@pytest.mark.asyncio
async def test_update_branch_with_sha_retry_non_sha_failure_no_retry(mocker):
    """Non-success but fresh SHA matches original → no retry, return original failure."""
    from app import github as gh

    update_branch_mock = mocker.patch.object(
        gh,
        "update_branch",
        new_callable=AsyncMock,
        return_value={"updated": False, "message": "merge conflict"},
    )
    mocker.patch.object(
        gh,
        "get_pull_request",
        new_callable=AsyncMock,
        return_value={"head": {"sha": "sameSha"}},
    )

    result = await gh.update_branch_with_sha_retry(
        "t",
        "o",
        "r",
        1,
        expected_head_sha="sameSha",
    )

    assert result == {"updated": False, "message": "merge conflict"}
    assert update_branch_mock.await_count == 1


@pytest.mark.asyncio
async def test_update_branch_with_sha_retry_retry_also_fails(mocker):
    """Retry with fresh SHA also fails → return the retry failure."""
    from app import github as gh

    update_branch_mock = mocker.patch.object(
        gh,
        "update_branch",
        new_callable=AsyncMock,
        side_effect=[
            {"updated": False, "message": "first failure"},
            {"updated": False, "message": "retry failure"},
        ],
    )
    mocker.patch.object(
        gh,
        "get_pull_request",
        new_callable=AsyncMock,
        return_value={"head": {"sha": "fresh999"}},
    )

    result = await gh.update_branch_with_sha_retry(
        "t",
        "o",
        "r",
        1,
        expected_head_sha="stale",
    )

    assert result == {"updated": False, "message": "retry failure"}
    assert update_branch_mock.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        "There are no new commits on the base branch.",
        "there are no new commits on the base branch",
        "  There are no new commits on the base branch.  ",
        "There are no new commits on the base branch",
    ],
)
async def test_update_branch_with_sha_retry_no_new_commits_tags_not_behind(mocker, msg):
    """GitHub's canonical 422 'no new commits on the base branch' must be tagged
    with not_behind=True so callers can short-circuit instead of treating it as
    a transient failure. Match is case/whitespace/period-tolerant."""
    from app import github as gh

    update_branch_mock = mocker.patch.object(
        gh,
        "update_branch",
        new_callable=AsyncMock,
        return_value={"updated": False, "message": msg},
    )
    get_pr_mock = mocker.patch.object(gh, "get_pull_request", new_callable=AsyncMock)

    result = await gh.update_branch_with_sha_retry(
        "t",
        "o",
        "r",
        1,
        expected_head_sha="abc",
    )

    assert result.get("not_behind") is True
    assert result.get("updated") is False
    # No retry — the not-behind tag should suppress the refetch+retry path.
    assert update_branch_mock.await_count == 1
    get_pr_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_branch_with_sha_retry_unrelated_422_does_not_tag(mocker):
    """Regression guard: an unrelated 422 message (e.g. 'Merge conflict') must
    NOT be tagged with not_behind. Only the canonical phrase triggers it."""
    from app import github as gh

    mocker.patch.object(
        gh,
        "update_branch",
        new_callable=AsyncMock,
        return_value={"updated": False, "message": "Merge conflict"},
    )
    mocker.patch.object(
        gh,
        "get_pull_request",
        new_callable=AsyncMock,
        return_value={"head": {"sha": "sameSha"}},
    )

    result = await gh.update_branch_with_sha_retry(
        "t",
        "o",
        "r",
        1,
        expected_head_sha="sameSha",
    )

    assert "not_behind" not in result
    assert result.get("updated") is False


@pytest.mark.asyncio
async def test_update_branch_with_sha_retry_no_new_commits_on_retry_tags_not_behind(
    mocker,
):
    """When the initial call fails with a generic message but the SHA-retry hits
    the canonical 'no new commits on base' 422, the retry result must still be
    tagged with not_behind=True."""
    from app import github as gh

    mocker.patch.object(
        gh,
        "update_branch",
        new_callable=AsyncMock,
        side_effect=[
            {"updated": False, "message": "stale SHA"},
            {
                "updated": False,
                "message": "There are no new commits on the base branch.",
            },
        ],
    )
    mocker.patch.object(
        gh,
        "get_pull_request",
        new_callable=AsyncMock,
        return_value={"head": {"sha": "fresh999"}},
    )

    result = await gh.update_branch_with_sha_retry(
        "t",
        "o",
        "r",
        1,
        expected_head_sha="stale",
    )

    assert result.get("not_behind") is True
    assert result.get("updated") is False


# ---------------------------------------------------------------------------
# build_merge_summary_body + post_merge_summary_comment
# ---------------------------------------------------------------------------


@pytest.mark.smoketest
def test_build_merge_summary_body_all_fields():
    """All fields produce a collapsible block with headline and detail lines."""
    from app.github import build_merge_summary_body

    body = build_merge_summary_body(
        task_type="create",
        scope="small",
        iteration_count=3,
        provider="anthropic",
        model="claude-opus-4-7",
        cost_usd=0.42,
        direction_title="Fix auth timeout",
        direction_id="abc12345",
    )

    assert "<details>" in body
    assert "create" in body
    assert "small" in body
    assert "3 review cycles" in body
    assert "~$0.42" in body
    assert "anthropic / claude-opus-4-7" in body
    assert "Fix auth timeout" in body
    assert "abc12345" in body


@pytest.mark.smoketest
def test_build_merge_summary_body_no_detail_lines_is_one_liner():
    """Without provider/model and without direction, body is a simple inline string."""
    from app.github import build_merge_summary_body

    body = build_merge_summary_body(
        task_type="fix", scope="medium", iteration_count=2, cost_usd=0.10
    )

    assert "<details>" not in body
    assert "🤖 Blueprint:" in body
    assert "fix" in body
    assert "medium" in body
    assert "2 review cycles" in body
    assert "~$0.10" in body


@pytest.mark.smoketest
def test_build_merge_summary_body_single_iteration_hidden():
    """iteration_count=1 is not shown (only meaningful for >1 cycles)."""
    from app.github import build_merge_summary_body

    body = build_merge_summary_body(task_type="create", iteration_count=1)
    assert "review cycle" not in body


@pytest.mark.smoketest
def test_build_merge_summary_body_empty_inputs():
    """Empty inputs fall back to 'merge complete' headline, no crash."""
    from app.github import build_merge_summary_body

    body = build_merge_summary_body()
    assert "merge complete" in body


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_post_merge_summary_comment_returns_true_on_success(mocker):
    """Happy path: posts comment and returns True."""
    from app.github import post_merge_summary_comment

    mock_comment = mocker.patch(
        "app.github.create_pr_comment", new_callable=AsyncMock, return_value={}
    )

    result = await post_merge_summary_comment(
        "token",
        "owner",
        "repo",
        42,
        task_type="create",
        scope="small",
        iteration_count=2,
        provider="anthropic",
        model="claude-opus-4-7",
        cost_usd=0.35,
        direction_title="Improve reliability",
    )

    assert result is True
    mock_comment.assert_awaited_once()
    body_arg = mock_comment.call_args[0][4]
    assert "create" in body_arg
    assert "Improve reliability" in body_arg


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_post_merge_summary_comment_returns_false_on_error(mocker):
    """If create_pr_comment raises, returns False instead of propagating."""
    from fastapi import HTTPException
    from app.github import post_merge_summary_comment

    mocker.patch(
        "app.github.create_pr_comment",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=500, detail="GitHub down"),
    )

    result = await post_merge_summary_comment("token", "owner", "repo", 42)

    assert result is False


# ---------------------------------------------------------------------------
# list_pull_requests_for_commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pull_requests_for_commit_success(mocker):
    """200 response with a list of PR dicts is returned as-is."""
    from app.github import list_pull_requests_for_commit

    prs = [
        {
            "number": 42,
            "state": "closed",
            "merged_at": "2026-01-01T00:00:00Z",
            "head": {"sha": "abc"},
        },
    ]
    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, prs),
    )

    result = await list_pull_requests_for_commit("tok", "owner", "repo", "deadbeef123")

    assert result == prs


@pytest.mark.asyncio
async def test_list_pull_requests_for_commit_non_200_returns_empty(mocker):
    """Non-200 response (e.g. 404) returns [] without raising."""
    from app.github import list_pull_requests_for_commit

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(404, {"message": "Not Found"}, text="Not Found"),
    )

    result = await list_pull_requests_for_commit("tok", "owner", "repo", "deadbeef123")

    assert result == []


@pytest.mark.asyncio
async def test_list_pull_requests_for_commit_non_list_body_returns_empty(mocker):
    """A 200 response whose body is not a list (e.g. a dict error) returns []."""
    from app.github import list_pull_requests_for_commit

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"message": "unexpected"}),
    )

    result = await list_pull_requests_for_commit("tok", "owner", "repo", "deadbeef123")

    assert result == []


@pytest.mark.asyncio
async def test_list_pull_requests_for_commit_paginates_all_pages(mocker):
    """list_pull_requests_for_commit must follow Link: rel=next headers and
    return all PRs across all pages.  A PR on page 2 whose head carries the
    failing CI check must not be silently dropped."""
    from app.github import list_pull_requests_for_commit

    page1 = [
        {
            "number": 1,
            "state": "closed",
            "merged_at": "2026-01-01T00:00:00Z",
            "head": {"sha": "sha_green_pr"},
        }
    ]
    page2 = [
        {
            "number": 2,
            "state": "closed",
            "merged_at": "2026-01-02T00:00:00Z",
            "head": {"sha": "sha_red_pr"},
        }
    ]

    call_count = 0

    def _mock_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockResponse(
                200,
                page1,
                links={
                    "next": {
                        "url": "https://api.github.com/repos/owner/repo/commits/sha/pulls?per_page=100&page=2"
                    }
                },
            )
        return MockResponse(200, page2)

    mocker.patch("httpx.AsyncClient.request", side_effect=_mock_request)

    result = await list_pull_requests_for_commit("tok", "owner", "repo", "sha_multi")

    assert len(result) == 2
    assert result[0]["number"] == 1
    assert result[1]["number"] == 2
    assert call_count == 2


# ---------------------------------------------------------------------------
# get_check_runs: treat_missing_as_success=False (strict mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_check_runs_strict_mode_empty_returns_no_checks(mocker):
    """With treat_missing_as_success=False an empty check-runs list returns
    status='no_checks' so callers can distinguish 'no CI' from 'CI passed'."""
    from app.github import get_check_runs

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"check_runs": []}),
    )

    result = await get_check_runs(
        "tok", "owner", "repo", "sha123", treat_missing_as_success=False
    )

    assert result["status"] == "no_checks"
    assert result["conclusion"] is None


@pytest.mark.asyncio
async def test_get_check_runs_default_mode_empty_returns_success(mocker):
    """Default (treat_missing_as_success=True): empty check-runs → status='completed',
    conclusion='success' to preserve the fail-open behaviour for all existing callers."""
    from app.github import get_check_runs

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"check_runs": []}),
    )

    result = await get_check_runs("tok", "owner", "repo", "sha123")

    assert result["status"] == "completed"
    assert result["conclusion"] == "success"


# ── get_files_changed_between_refs ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_files_changed_between_refs_returns_changed_files(mocker):
    """Returns filenames from the compare API when it succeeds."""
    from app.github import get_files_changed_between_refs

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(
            200,
            {
                "files": [
                    {"filename": "backend/app/foo.py"},
                    {"filename": "backend/app/bar.py"},
                ]
            },
        ),
    )

    result = await get_files_changed_between_refs(
        "tok", "owner", "repo", "base-ref", "head-ref"
    )
    assert result == ["backend/app/foo.py", "backend/app/bar.py"]


@pytest.mark.asyncio
async def test_get_files_changed_between_refs_returns_empty_when_identical(mocker):
    """Returns an empty list when the compare API reports no changed files."""
    from app.github import get_files_changed_between_refs

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"files": []}),
    )

    result = await get_files_changed_between_refs(
        "tok", "owner", "repo", "automain", "automain"
    )
    assert result == []


@pytest.mark.asyncio
async def test_get_files_changed_between_refs_returns_none_on_api_error(mocker):
    """Returns None on a non-200 API response so callers can treat it as indeterminate."""
    from app.github import get_files_changed_between_refs

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(404, {}),
    )

    result = await get_files_changed_between_refs(
        "tok", "owner", "repo", "missing-ref", "head"
    )
    assert result is None


@pytest.mark.asyncio
async def test_is_ancestor_of_returns_true_when_ahead(mocker):
    """Returns True when the compare API reports descendant_ref is ahead of ancestor_sha."""
    from app.github import is_ancestor_of

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"status": "ahead"}),
    )

    result = await is_ancestor_of("tok", "owner", "repo", "abc123", "automain")
    assert result is True


@pytest.mark.asyncio
async def test_is_ancestor_of_returns_true_when_identical(mocker):
    """Returns True when ancestor_sha and descendant_ref point at the same commit."""
    from app.github import is_ancestor_of

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"status": "identical"}),
    )

    result = await is_ancestor_of("tok", "owner", "repo", "abc123", "automain")
    assert result is True


@pytest.mark.asyncio
async def test_is_ancestor_of_returns_false_when_not_merged_to_integration_branch(
    mocker,
):
    """Returns False when fix PR merged only to a staging branch (not the integration branch).

    A rollup child PR that merged to a staging branch has status 'behind' or 'diverged'
    relative to the integration branch — the fix has NOT landed and recovery must NOT fire.
    """
    from app.github import is_ancestor_of

    for not_ancestor_status in ("behind", "diverged"):
        mocker.patch(
            "httpx.AsyncClient.request",
            return_value=MockResponse(200, {"status": not_ancestor_status}),
        )
        result = await is_ancestor_of("tok", "owner", "repo", "abc123", "automain")
        assert result is False, f"expected False for status={not_ancestor_status!r}"


@pytest.mark.asyncio
async def test_is_ancestor_of_returns_none_on_api_error(mocker):
    """Returns None (not False) on a non-200 API response — indeterminate result degrades safely.

    An indeterminate result must NOT be treated as 'landed'; the regression case stays
    eligible for re-spawn rather than being silently confirmed as fixed.
    """
    from app.github import is_ancestor_of

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(500, {}),
    )

    result = await is_ancestor_of("tok", "owner", "repo", "abc123", "automain")
    assert result is None


@pytest.mark.asyncio
async def test_is_ancestor_of_returns_none_on_unparseable_response(mocker):
    """Returns None when the compare API body cannot be parsed — degrades safely, not False."""
    from app.github import is_ancestor_of

    bad_response = MockResponse(200, None)
    bad_response._json_data = None  # simulate json() returning None

    def _raise():
        raise ValueError("no JSON")

    bad_response.json = _raise
    mocker.patch("httpx.AsyncClient.request", return_value=bad_response)

    result = await is_ancestor_of("tok", "owner", "repo", "abc123", "automain")
    assert result is None


# ---------------------------------------------------------------------------
# pr_is_not_behind — GitHub-clean / ancestor-of-base detector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_is_not_behind_clean_mergeable_state_returns_true():
    """mergeable_state='clean' is the authoritative GitHub signal — returns True
    immediately without any API call, even when head and base SHAs differ."""
    from app.github import pr_is_not_behind

    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        head_sha="head_sha_abc",
        base_tip_sha="base_tip_xyz",
        mergeable_state="clean",
    )
    assert result is True


@pytest.mark.asyncio
async def test_pr_is_not_behind_clean_state_no_sha_still_returns_true():
    """mergeable_state='clean' short-circuits even with no SHAs provided."""
    from app.github import pr_is_not_behind

    result = await pr_is_not_behind("tok", "owner", "repo", mergeable_state="clean")
    assert result is True


@pytest.mark.asyncio
async def test_pr_is_not_behind_ancestor_check_true_returns_true(mocker):
    """base_tip_sha is already reachable from head_sha (status=ahead) → not behind."""
    from app.github import pr_is_not_behind

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"status": "ahead"}),
    )

    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        head_sha="head_abc",
        base_tip_sha="base_xyz",
    )
    assert result is True


@pytest.mark.asyncio
async def test_pr_is_not_behind_ancestor_check_false_returns_false(mocker):
    """base_tip_sha is NOT reachable from head_sha (status=behind) → PR is behind."""
    from app.github import pr_is_not_behind

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(200, {"status": "behind"}),
    )

    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        head_sha="head_abc",
        base_tip_sha="base_xyz",
    )
    assert result is False


@pytest.mark.asyncio
async def test_pr_is_not_behind_api_error_falls_through_to_behind_state(mocker):
    """When is_ancestor_of returns None (API error) and mergeable_state='behind',
    falls through to mergeable_state signal and returns False."""
    from app.github import pr_is_not_behind

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(500, {}),
    )

    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        head_sha="head_abc",
        base_tip_sha="base_xyz",
        mergeable_state="behind",
    )
    assert result is False


@pytest.mark.asyncio
async def test_pr_is_not_behind_api_error_no_fallback_returns_none(mocker):
    """When is_ancestor_of returns None (API error) and no other signal is available,
    returns None (indeterminate) — does not default to False."""
    from app.github import pr_is_not_behind

    mocker.patch(
        "httpx.AsyncClient.request",
        return_value=MockResponse(500, {}),
    )

    result = await pr_is_not_behind(
        "tok",
        "owner",
        "repo",
        head_sha="head_abc",
        base_tip_sha="base_xyz",
    )
    assert result is None


@pytest.mark.asyncio
async def test_pr_is_not_behind_no_shas_behind_state_returns_false():
    """No SHAs provided, mergeable_state='behind' → returns False."""
    from app.github import pr_is_not_behind

    result = await pr_is_not_behind("tok", "owner", "repo", mergeable_state="behind")
    assert result is False


@pytest.mark.asyncio
async def test_pr_is_not_behind_no_shas_no_state_returns_none():
    """No SHAs, no mergeable_state → indeterminate (None)."""
    from app.github import pr_is_not_behind

    result = await pr_is_not_behind("tok", "owner", "repo")
    assert result is None


@pytest.mark.asyncio
async def test_get_repo_default_branch_returns_branch_on_200(mocker):
    """200 with default_branch in JSON returns the branch name."""
    from app.github import get_repo_default_branch

    mocker.patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        return_value=MockResponse(200, {"default_branch": "trunk"}),
    )

    result = await get_repo_default_branch("tok", "acme", "repo")
    assert result == "trunk"


@pytest.mark.asyncio
async def test_get_repo_default_branch_non200_raises_502(mocker):
    """Non-200 GitHub API response raises HTTPException(502) instead of silently returning 'main'."""
    from app.github import get_repo_default_branch

    mocker.patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        return_value=MockResponse(403, text="Forbidden"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_repo_default_branch("tok", "acme", "repo")
    assert exc_info.value.status_code == 502
    assert "HTTP 403" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_repo_default_branch_missing_key_raises_502(mocker):
    """200 response missing default_branch field raises HTTPException(502) instead of substituting 'main'."""
    from app.github import get_repo_default_branch

    mocker.patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        return_value=MockResponse(200, {"name": "repo", "owner": {"login": "acme"}}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_repo_default_branch("tok", "acme", "repo")
    assert exc_info.value.status_code == 502
    assert "default_branch" in exc_info.value.detail
