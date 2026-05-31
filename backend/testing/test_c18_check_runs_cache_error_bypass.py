"""Regression tests for cluster C18: get_check_runs_cached must NOT cache
API-error results (status='pending' with _from_api_error=True), and
list_pull_requests_for_commit must deduplicate calls with a 30 s TTL.

Without the fix:
  * A GitHub 5xx / rate-limit response causes get_check_runs to return a fake
    'pending' dict, which get_check_runs_cached stores unconditionally for 30 s.
  * Every sibling task watching the same SHA then gets a stale fake-pending for
    the rest of the TTL window, delaying CI-completion detection exactly when
    the system is most degraded.

With the fix:
  * get_check_runs tags non-200 results with ``_from_api_error=True``.
  * get_check_runs_cached returns the result directly without caching it.
  * The next call retries the real GitHub lookup rather than serving stale data.
  * list_pull_requests_for_commit caches successful results on a 30 s TTL,
    deduplicating sibling calls — but does NOT cache error (non-200) results.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_data=None, links=None, text=""):
    """Minimal mock for an httpx-like response object."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data if json_data is not None else {}
    mock.links = links or {}
    mock.text = text
    return mock


# ---------------------------------------------------------------------------
# C18a: get_check_runs tags non-200 responses with _from_api_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_check_runs_non200_sets_from_api_error_flag():
    """A non-200 GitHub response returns a dict with _from_api_error=True so
    the caching layer can identify and skip it."""
    from app import github_checks

    error_response = _make_response(503, text="Service Unavailable")

    with patch.object(github_checks, "_github_request", new=AsyncMock(return_value=error_response)):
        result = await github_checks.get_check_runs(
            token="tok", owner="o", repo="r", ref="abc123"
        )

    assert result.get("_from_api_error") is True
    assert result["status"] == "pending"
    assert result["conclusion"] is None


@pytest.mark.asyncio
async def test_get_check_runs_200_does_not_set_from_api_error_flag():
    """A successful GitHub response does NOT set _from_api_error."""
    from app import github_checks

    ok_response = _make_response(
        200,
        json_data={
            "check_runs": [
                {
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "id": 1,
                }
            ]
        },
        links={},
    )

    with patch.object(github_checks, "_github_request", new=AsyncMock(return_value=ok_response)):
        result = await github_checks.get_check_runs(
            token="tok", owner="o", repo="r", ref="abc123"
        )

    assert "_from_api_error" not in result
    assert result["status"] == "completed"
    assert result["conclusion"] == "success"


# ---------------------------------------------------------------------------
# C18b: get_check_runs_cached skips caching error results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_check_runs_cached_does_not_cache_api_error_result():
    """When get_check_runs returns an _from_api_error result, get_check_runs_cached
    must NOT store it — a subsequent call must hit GitHub again, not serve the
    stale error dict from the local cache."""
    from app import github_checks, github as _gh

    error_result = {
        "status": "pending",
        "conclusion": None,
        "details": [],
        "failed_logs": "",
        "_from_api_error": True,
    }
    ok_result = {
        "status": "completed",
        "conclusion": "success",
        "details": [{"name": "ci", "status": "completed", "conclusion": "success"}],
        "failed_logs": "",
    }

    # Flush the module-level cache so previous test runs don't interfere.
    github_checks._check_runs_cache.clear()

    call_count = 0

    async def _fake_get_check_runs(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return error_result
        return ok_result

    with patch.object(_gh, "get_check_runs", side_effect=_fake_get_check_runs):
        result1 = await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha_error_test"
        )
        # First call: returned the error result.
        assert result1.get("_from_api_error") is True

        # Second call with the same SHA: must NOT serve the cached error — must
        # call get_check_runs again and get the real result.
        result2 = await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha_error_test"
        )
        assert result2["status"] == "completed"
        assert result2["conclusion"] == "success"

    assert call_count == 2, (
        "get_check_runs should have been called twice — the error result "
        "must not have been served from cache on the second call"
    )


@pytest.mark.asyncio
async def test_get_check_runs_cached_caches_successful_result():
    """A successful (non-error) result IS cached: a second call within the TTL
    must NOT call get_check_runs again."""
    from app import github_checks, github as _gh

    ok_result = {
        "status": "completed",
        "conclusion": "success",
        "details": [],
        "failed_logs": "",
    }

    github_checks._check_runs_cache.clear()

    call_count = 0

    async def _fake_get_check_runs(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return ok_result

    with patch.object(_gh, "get_check_runs", side_effect=_fake_get_check_runs):
        result1 = await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha_ok_cache_test"
        )
        result2 = await github_checks.get_check_runs_cached(
            "tok", "o", "r", "sha_ok_cache_test"
        )

    assert result1["status"] == "completed"
    assert result2["status"] == "completed"
    assert call_count == 1, (
        "get_check_runs should only be called once — the second call must be "
        "served from the cache"
    )


@pytest.mark.asyncio
async def test_get_check_runs_cached_error_result_not_present_in_cache():
    """After an API-error call, the SHA must be absent from the cache dict so
    the next caller with the same key gets a fresh lookup."""
    from app import github_checks, github as _gh

    error_result = {
        "status": "pending",
        "conclusion": None,
        "details": [],
        "failed_logs": "",
        "_from_api_error": True,
    }

    github_checks._check_runs_cache.clear()
    sha = "sha_absent_after_error"

    with patch.object(_gh, "get_check_runs", new=AsyncMock(return_value=error_result)):
        await github_checks.get_check_runs_cached("tok", "o", "r", sha)

    cache_key = ("o", "r", sha, True)
    assert cache_key not in github_checks._check_runs_cache, (
        "The cache must NOT contain the API-error result — it should be absent "
        "so the next call retries the real GitHub lookup"
    )


# ---------------------------------------------------------------------------
# C18c: list_pull_requests_for_commit caches successful results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pull_requests_for_commit_caches_success():
    """Successful results are cached on a 30 s TTL; a second call within the
    window must not hit GitHub again."""
    from app import github_refs

    pr_list = [{"number": 42, "merged_at": "2026-01-01T00:00:00Z", "head": {"sha": "abc"}}]
    ok_response = _make_response(200, json_data=pr_list, links={})

    github_refs._list_prs_for_commit_cache.clear()

    call_count = 0

    async def _fake_github_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return ok_response

    with patch.object(github_refs, "_github_request", side_effect=_fake_github_request):
        result1 = await github_refs.list_pull_requests_for_commit(
            "tok", "o", "r", "sha_prs_cache"
        )
        result2 = await github_refs.list_pull_requests_for_commit(
            "tok", "o", "r", "sha_prs_cache"
        )

    assert result1 == pr_list
    assert result2 == pr_list
    assert call_count == 1, (
        "list_pull_requests_for_commit should only call GitHub once within the "
        "30 s TTL — second call must be served from cache"
    )


@pytest.mark.asyncio
async def test_list_pull_requests_for_commit_does_not_cache_error():
    """A non-200 response returns [] without caching — the next call must retry
    rather than serve the stale empty list."""
    from app import github_refs

    error_response = _make_response(503, text="Service Unavailable")
    ok_response = _make_response(
        200,
        json_data=[{"number": 1, "head": {"sha": "abc"}}],
        links={},
    )

    github_refs._list_prs_for_commit_cache.clear()

    responses = [error_response, ok_response]
    call_count = 0

    async def _fake_github_request(*args, **kwargs):
        nonlocal call_count
        resp = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return resp

    with patch.object(github_refs, "_github_request", side_effect=_fake_github_request):
        result1 = await github_refs.list_pull_requests_for_commit(
            "tok", "o", "r", "sha_prs_error"
        )
        # Error → empty list returned but NOT cached.
        assert result1 == []

        result2 = await github_refs.list_pull_requests_for_commit(
            "tok", "o", "r", "sha_prs_error"
        )
        # Second call must retry GitHub and get the real result.
        assert result2 == [{"number": 1, "head": {"sha": "abc"}}]

    assert call_count == 2, (
        "list_pull_requests_for_commit must retry GitHub after an error — "
        "the error result must not have been cached"
    )


@pytest.mark.asyncio
async def test_list_pull_requests_for_commit_cache_expires():
    """After the TTL expires, a subsequent call must fetch from GitHub again."""
    from app import github_refs

    pr_list = [{"number": 5, "head": {"sha": "deadbeef"}}]
    ok_response = _make_response(200, json_data=pr_list, links={})

    github_refs._list_prs_for_commit_cache.clear()

    call_count = 0

    async def _fake_github_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return ok_response

    sha = "sha_expire_test"
    with patch.object(github_refs, "_github_request", side_effect=_fake_github_request):
        # Populate the cache.
        await github_refs.list_pull_requests_for_commit("tok", "o", "r", sha)
        assert call_count == 1

        # Manually expire the cache entry.
        key = ("o", "r", sha)
        old_val, _ = github_refs._list_prs_for_commit_cache[key]
        github_refs._list_prs_for_commit_cache[key] = (old_val, 0.0)

        # Call again — must bypass the expired entry.
        await github_refs.list_pull_requests_for_commit("tok", "o", "r", sha)
        assert call_count == 2, "Expired cache entry must not be served"
