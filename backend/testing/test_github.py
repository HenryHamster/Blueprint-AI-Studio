import pytest
from httpx import AsyncClient
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

    mock_get = mocker.patch("httpx.AsyncClient.get")
    mock_get.return_value = MockResponse(200, {"login": "testuser"})

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

    mock_get = mocker.patch("httpx.AsyncClient.get")
    mock_get.return_value = MockResponse(401, {"message": "Bad credentials"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_fine_grained_pat("github_pat_expired_token_123")

    assert exc_info.value.status_code == 401
    assert "invalid or expired" in exc_info.value.detail


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

    mock_put = mocker.patch("httpx.AsyncClient.put")
    mock_put.return_value = MockResponse(
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

    mock_put = mocker.patch("httpx.AsyncClient.put")
    mock_put.return_value = MockResponse(
        409,
        {"message": "Merge conflict"},
        text="Merge conflict",
    )

    result = await merge_pull_request("token", "owner", "repo", 42)

    assert result["merged"] is False
    assert result["conflict"] is True
    assert "message" in result


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

    async def mock_get(*args, **kwargs):
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

    mocker.patch("httpx.AsyncClient.get", side_effect=mock_get)

    result = await get_check_runs("token", "owner", "repo", "abc123sha")

    assert result["status"] == "completed"
    assert result["conclusion"] == "failure"
    assert "failed_logs" in result
    # Fallback path should include some failure context
    assert result["failed_logs"] != ""


@pytest.mark.asyncio
async def test_merge_pull_request_rate_limit_propagates_error(mocker):
    """
    A 429 rate-limit response (with Retry-After header) should raise an
    HTTPException rather than being silently swallowed.
    """
    from app.github import merge_pull_request

    mock_put = mocker.patch("httpx.AsyncClient.put")
    mock_put.return_value = MockResponse(
        429,
        {"message": "API rate limit exceeded"},
        text="API rate limit exceeded",
        headers={"Retry-After": "60"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await merge_pull_request("token", "owner", "repo", 42)

    # Must not surface as a generic 200/non-error; a 5xx HTTPException is raised
    assert exc_info.value.status_code >= 400


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_validate_fine_grained_pat_expired_token_returns_401_not_500(mocker):
    """
    validate_fine_grained_pat() with an expired token (GitHub returns 401)
    should raise HTTPException(401), not HTTPException(500).
    """
    from app.github import validate_fine_grained_pat

    mock_get = mocker.patch("httpx.AsyncClient.get")
    mock_get.return_value = MockResponse(
        401,
        {"message": "Token has expired"},
        text="Token has expired",
    )

    with pytest.raises(HTTPException) as exc_info:
        await validate_fine_grained_pat("github_pat_expired_xyz_abc_123")

    assert exc_info.value.status_code == 401
    assert "invalid or expired" in exc_info.value.detail
