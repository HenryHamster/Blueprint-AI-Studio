"""Tests for create-PR idempotency (fail-closed under GitHub rate limiting).

WI-3 (orchestrator error-budget remediation): the pre-create open-PR lookup
used to fail OPEN — a rate-limited / short-circuited ``GET /pulls`` returned
``None`` ("no existing PR"), so ``create_pull_request`` POSTed anyway and
created a DUPLICATE PR once the drained GitHub quota reset. It now fails CLOSED:
an unconfirmed lookup refuses to create and raises a retryable HTTP 503 +
``Retry-After`` so the agent backs off instead.
"""

import time

import pytest
from fastapi import HTTPException


class MockResponse:
    """Minimal stand-in for an httpx response used by ``_github_request``."""

    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data


def _rate_limited_response(reset_in_s: int = 300) -> MockResponse:
    """A synthetic 403 mirroring the local limiter's short-circuit response."""
    return MockResponse(
        403,
        {"message": "rate limit exceeded (short-circuited locally)"},
        text="rate limit exceeded (short-circuited locally)",
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Reset": str(int(time.time()) + reset_in_s),
        },
    )


@pytest.mark.asyncio
async def test_create_pr_refuses_when_lookup_rate_limited(mocker):
    """A rate-limited/short-circuited open-PR lookup must REFUSE the create with
    a retryable 503 + Retry-After — NOT fail open and POST a duplicate."""
    from app import github_pulls

    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=_rate_limited_response(reset_in_s=300)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token", "owner", "repo", "Title", "feature-head", "main"
        )

    exc = exc_info.value
    assert exc.status_code == 503
    # Retry-After present and a positive integer (capped at the module ceiling).
    retry_after = int((exc.headers or {}).get("Retry-After"))
    assert 0 < retry_after <= github_pulls._CREATE_PR_MAX_RETRY_AFTER_S
    # Only the lookup GET happened — the create POST was NEVER sent.
    assert mock_request.call_count == 1
    assert mock_request.call_args_list[0].args[0] == "GET"


@pytest.mark.asyncio
async def test_create_pr_proceeds_when_lookup_confirms_none(mocker):
    """A confirmed 'no existing PR' (real HTTP 200 empty list) lets the create
    proceed and returns the newly created PR."""
    from app import github_pulls

    created_pr = {"number": 5, "html_url": "https://github.com/owner/repo/pull/5"}
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, []),  # lookup: confirmed none
                MockResponse(201, created_pr),  # POST: created
            ]
        ),
    )

    result = await github_pulls.create_pull_request(
        "ghs_token", "owner", "repo", "Title", "feature-head", "main"
    )

    assert result == created_pr
    assert mock_request.call_count == 2
    assert mock_request.call_args_list[0].args[0] == "GET"
    assert mock_request.call_args_list[1].args[0] == "POST"


@pytest.mark.asyncio
async def test_create_pr_dedups_to_existing_open_pr(mocker):
    """An existing open PR (real HTTP 200 with a PR) is returned as-is; the
    create POST is never sent."""
    from app import github_pulls

    existing_pr = {"number": 9, "html_url": "https://github.com/owner/repo/pull/9"}
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, [existing_pr])),
    )

    result = await github_pulls.create_pull_request(
        "ghs_token", "owner", "repo", "Title", "feature-head", "main"
    )

    assert result == existing_pr
    # Dedup short-circuits before any POST.
    assert mock_request.call_count == 1
    assert mock_request.call_args_list[0].args[0] == "GET"


@pytest.mark.asyncio
async def test_create_pr_refuses_when_post_rate_limited_after_confirmed_none(mocker):
    """Even when the lookup confirms 'none', if the create POST itself is
    rate-limited the create did not happen — surface a retryable 503, not a 500."""
    from app import github_pulls

    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, []),  # lookup: confirmed none
                _rate_limited_response(reset_in_s=60),  # POST: rate-limited
            ]
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token", "owner", "repo", "Title", "feature-head", "main"
        )

    assert exc_info.value.status_code == 503
    assert int((exc_info.value.headers or {}).get("Retry-After")) > 0
    assert mock_request.call_count == 2


@pytest.mark.asyncio
async def test_create_pr_refuses_when_lookup_raises(mocker):
    """A raised lookup (network/transport error) is UNCERTAIN — refuse, don't
    fail open. Retry-After falls back to the module default when underivable."""
    from app import github_pulls

    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(side_effect=RuntimeError("connection reset")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.create_pull_request(
            "ghs_token", "owner", "repo", "Title", "feature-head", "main"
        )

    assert exc_info.value.status_code == 503
    assert (
        int((exc_info.value.headers or {}).get("Retry-After"))
        == github_pulls._CREATE_PR_DEFAULT_RETRY_AFTER_S
    )
    # No POST was attempted after the lookup blew up.
    assert mock_request.call_count == 1


def test_retry_after_seconds_prefers_header_then_reset_then_none():
    """Unit-cover the Retry-After derivation used for the 503 backoff hint."""
    from app import github_pulls

    # Explicit Retry-After header wins.
    assert (
        github_pulls._retry_after_seconds_from_response(
            MockResponse(403, headers={"Retry-After": "42"})
        )
        == 42
    )
    # Falls back to X-RateLimit-Reset delta.
    reset = MockResponse(
        403, headers={"X-RateLimit-Reset": str(int(time.time()) + 90)}
    )
    derived = github_pulls._retry_after_seconds_from_response(reset)
    assert derived is not None and 80 <= derived <= 90
    # Neither present → None (caller substitutes the default).
    assert github_pulls._retry_after_seconds_from_response(MockResponse(403)) is None
