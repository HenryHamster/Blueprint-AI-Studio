"""Tests for create-PR idempotency (fail-closed under GitHub rate limiting).

WI-3 (orchestrator error-budget remediation): the pre-create open-PR lookup
used to fail OPEN — a rate-limited / short-circuited ``GET /pulls`` returned
``None`` ("no existing PR"), so ``create_pull_request`` POSTed anyway and
created a DUPLICATE PR once the drained GitHub quota reset. It now fails CLOSED:
an unconfirmed lookup refuses to create and raises a retryable HTTP 503 +
``Retry-After`` so the agent backs off instead.

Also covers ``merge_pull_request`` (same rate-limit classification, body parsed
only behind the status check) and ``get_pull_request_reviews_checked`` pagination.
"""

import ast
import time
from pathlib import Path
from unittest.mock import AsyncMock

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


class HtmlBodyResponse(MockResponse):
    """A response whose body is NOT JSON — GitHub's edge serves an HTML error
    page on many 5xx. ``.json()`` raises exactly as httpx's does."""

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("merged_payload", "expected_merged"),
    [
        ({"merged": True}, True),
        ({"merged": False}, False),
        ({}, None),
        ({"merged": "false"}, None),
    ],
)
async def test_fetch_live_review_target_preserves_merged_tristate(
    mocker, merged_payload, expected_merged
):
    from app.ci_merge_helpers import _fetch_live_review_target

    get_pull_request = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                **merged_payload,
                "state": "open",
                "base": {"ref": "automain", "sha": "base-42"},
                "head": {
                    "ref": "blueprint/operator-approval",
                    "sha": "head-42",
                },
            }
        ),
    )

    target = await _fetch_live_review_target(
        "token", "owner", "repo", 42, project_id="project-42"
    )

    assert target["merged"] is expected_merged
    get_pull_request.assert_awaited_once()


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
    from app import github_http, github_pulls

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
    assert 0 < retry_after <= github_http.RATE_LIMIT_MAX_RETRY_AFTER_S
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
    from app import github_http, github_pulls

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
        == github_http.RATE_LIMIT_DEFAULT_RETRY_AFTER_S
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
    reset = MockResponse(403, headers={"X-RateLimit-Reset": str(int(time.time()) + 90)})
    derived = github_pulls._retry_after_seconds_from_response(reset)
    assert derived is not None and 80 <= derived <= 90
    # Neither present → None (caller substitutes the default).
    assert github_pulls._retry_after_seconds_from_response(MockResponse(403)) is None


# ─────────────────────────────────────────────────────────────────────────────
# merge_pull_request — rate limits are RETRYABLE, body parsed behind the status
#
# ``_github_request`` deliberately RETURNS the raw 403/429 on a hard rate limit
# (reset window past the fast-fail threshold) instead of sleeping through it.
# merge_pull_request used to funnel that into a generic HTTPException(500) with
# no Retry-After — so a drained quota bucket read downstream as a HARD merge
# failure: spurious fallback-review tasks (task_lifecycle) and false "blocked"
# operator alerts (hotfix_backmerge). It must classify the shape exactly as
# create_pull_request does.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_pr_hard_rate_limit_403_raises_retryable_503(mocker):
    """A hard-rate-limited merge PUT (403 + X-RateLimit-Remaining: 0) is a
    RETRYABLE 503 + Retry-After — the merge never reached GitHub."""
    from app import github_http, github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=_rate_limited_response(reset_in_s=90)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.merge_pull_request(
            "ghs_token", "owner", "repo", 42, allow_changes_requested=True
        )

    exc = exc_info.value
    assert exc.status_code == 503, "rate limit must NOT be reported as a hard 500"
    retry_after = int((exc.headers or {}).get("Retry-After"))
    assert 0 < retry_after <= github_http.RATE_LIMIT_MAX_RETRY_AFTER_S


@pytest.mark.asyncio
async def test_merge_pr_secondary_rate_limit_429_raises_retryable_503(mocker):
    """429 (secondary/abuse limit) is likewise retryable, and the advertised
    Retry-After comes from GitHub's own header."""
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(
                429,
                {"message": "You have exceeded a secondary rate limit"},
                text="secondary rate limit",
                headers={"Retry-After": "45"},
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.merge_pull_request(
            "ghs_token", "owner", "repo", 7, allow_changes_requested=True
        )

    assert exc_info.value.status_code == 503
    assert (exc_info.value.headers or {}).get("Retry-After") == "45"


@pytest.mark.asyncio
async def test_merge_pr_forwards_expected_head_sha_as_merge_guard(mocker):
    """expected_head_sha must be sent as GitHub's merge `sha` guard so a head
    that advanced after authorisation fails closed (409) instead of merging."""
    from app import github_pulls

    ok = HtmlBodyResponse(200, text='{"merged": true}')
    request = mocker.AsyncMock(return_value=ok)
    mocker.patch("app.github._github_request", new=request)

    await github_pulls.merge_pull_request(
        "ghs_token", "owner", "repo", 42,
        allow_changes_requested=True,
        expected_head_sha="abc1234",
    )
    body = request.await_args.kwargs["json"]
    assert body["sha"] == "abc1234"

    # Without an expected sha, no guard is sent (unchanged behaviour).
    request2 = mocker.AsyncMock(return_value=HtmlBodyResponse(200, text='{"merged": true}'))
    mocker.patch("app.github._github_request", new=request2)
    await github_pulls.merge_pull_request(
        "ghs_token", "owner", "repo", 42, allow_changes_requested=True
    )
    assert "sha" not in request2.await_args.kwargs["json"]


@pytest.mark.asyncio
async def test_merge_pr_put_disables_transport_retries(mocker):
    """An approval PUT can be ambiguous; the transport must never replay it."""

    from app import github_pulls

    request = mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=MockResponse(409, {"message": "conflict"})),
    )

    await github_pulls.merge_pull_request(
        "t", "o", "r", 42, allow_changes_requested=True
    )

    assert request.await_args.kwargs["max_retries"] == 1


@pytest.mark.asyncio
async def test_merge_pr_html_5xx_body_does_not_raise_jsondecodeerror(mocker):
    """An HTML 5xx body must surface as a normal HTTPException, NOT a raw
    JSONDecodeError.

    The body used to be parsed BEFORE the status check (``data =
    response.json()`` on line 1 of the handler), so GitHub's Cloudflare error
    page blew up with a ValueError that no caller in the pipeline handles.
    """
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=HtmlBodyResponse(
                502, text="<html><body>502 Bad Gateway</body></html>"
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.merge_pull_request(
            "ghs_token", "owner", "repo", 99, allow_changes_requested=True
        )

    assert exc_info.value.status_code == 500
    assert "502" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_merge_pr_html_body_on_405_still_returns_blocked_shape(mocker):
    """Even a 405 with an unparseable body returns the documented
    {"merged": False, ...} contract instead of exploding."""
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=HtmlBodyResponse(405, text="<html>nope")),
    )

    result = await github_pulls.merge_pull_request(
        "ghs_token", "owner", "repo", 12, allow_changes_requested=True
    )

    assert result == {"merged": False, "conflict": False, "message": "Merge failed"}


@pytest.mark.asyncio
async def test_merge_pr_success_and_conflict_shapes_unchanged(mocker):
    """Regression guard: the 200 / 409 contracts every caller depends on are
    untouched by the rate-limit + safe-parse changes."""
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(
                200, {"message": "Merged", "sha": "merge-receipt-sha"}
            )
        ),
    )
    assert await github_pulls.merge_pull_request(
        "t", "o", "r", 1, allow_changes_requested=True
    ) == {
        "merged": True,
        "message": "Merged",
        "sha": "merge-receipt-sha",
    }

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(409, {"message": "Merge conflict"})
        ),
    )
    assert await github_pulls.merge_pull_request(
        "t", "o", "r", 2, allow_changes_requested=True
    ) == {
        "merged": False,
        "conflict": True,
        "message": "Merge conflict",
    }


def test_changes_requested_override_is_limited_to_explicit_operator_lanes():
    """Autonomous merge callers may not opt out of the external-review veto."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    overrides = set()

    def _calls_owned_by(function):
        """Yield calls in *function* without attributing nested callbacks twice."""
        pending = list(function.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Call):
                yield node
            pending.extend(ast.iter_child_nodes(node))

    for source_path in app_dir.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for call in _calls_owned_by(function):
                called = (
                    call.func.id
                    if isinstance(call.func, ast.Name)
                    else call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else ""
                )
                if called != "merge_pull_request":
                    continue
                override = next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == "allow_changes_requested"
                    ),
                    None,
                )
                if override is None:
                    continue
                assert isinstance(override, ast.Constant) and override.value is True
                overrides.add(
                    (source_path.relative_to(app_dir).as_posix(), function.name)
                )

    assert overrides == {
        ("admin_approval_followup_handlers.py", "_submit_human_rollup_merge"),
        ("admin_approval_followup_handlers.py", "_submit_human_critical_merge"),
    }

@pytest.mark.asyncio
async def test_merge_pr_default_vetoes_changes_requested_before_put(mocker):
    from app import github_pulls

    decision_mock = mocker.patch(
        "app.github_pulls.get_pull_request_review_decision",
        new=AsyncMock(return_value="CHANGES_REQUESTED"),
    )
    request_mock = mocker.patch("app.github._github_request", new=AsyncMock())

    result = await github_pulls.merge_pull_request("t", "o", "r", 42)

    assert result == {
        "merged": False,
        "conflict": False,
        "review_veto": True,
        "message": "GitHub has outstanding changes requested",
    }
    decision_mock.assert_awaited_once()
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [None, "APPROVED"])
async def test_merge_pr_cleared_or_absent_review_decision_allows_put(mocker, decision):
    from app import github_pulls

    mocker.patch(
        "app.github_pulls.get_pull_request_review_decision",
        new=AsyncMock(return_value=decision),
    )
    request_mock = mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=MockResponse(200, {"message": "Merged"})),
    )

    result = await github_pulls.merge_pull_request("t", "o", "r", 42)

    assert result["merged"] is True
    assert request_mock.await_args.args[0] == "PUT"


@pytest.mark.asyncio
async def test_merge_pr_review_decision_lookup_failure_never_reaches_put(mocker):
    from app import github_pulls

    mocker.patch(
        "app.github_pulls.get_pull_request_review_decision",
        new=AsyncMock(
            side_effect=HTTPException(status_code=502, detail="unavailable")
        ),
    )
    request_mock = mocker.patch("app.github._github_request", new=AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.merge_pull_request("t", "o", "r", 42)

    assert exc_info.value.status_code == 502
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_pr_operator_override_is_explicit_and_skips_veto_lookup(mocker):
    from app import github_pulls

    decision_mock = mocker.patch(
        "app.github_pulls.get_pull_request_review_decision", new=AsyncMock()
    )
    request_mock = mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=MockResponse(200, {"message": "Merged"})),
    )

    result = await github_pulls.merge_pull_request(
        "t", "o", "r", 42, allow_changes_requested=True
    )

    assert result["merged"] is True
    decision_mock.assert_not_awaited()
    assert request_mock.await_args.args[0] == "PUT"

@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [None, "APPROVED", "CHANGES_REQUESTED"])
async def test_get_pull_request_review_decision_reads_graphql_aggregate(
    mocker, decision
):
    from app import github_pulls

    mock_request = mocker.patch(
        "app.github._github_request",
        new=AsyncMock(
            return_value=MockResponse(
                200,
                {
                    "data": {
                        "repository": {"pullRequest": {"reviewDecision": decision}}
                    }
                },
            )
        ),
    )

    result = await github_pulls.get_pull_request_review_decision(
        "t", "owner", "repo", 42, project_id="project-id"
    )

    assert result == decision
    request = mock_request.call_args
    assert request.args[:2] == ("POST", "https://api.github.com/graphql")
    assert request.kwargs["json"]["variables"] == {
        "owner": "owner",
        "repo": "repo",
        "number": 42,
    }


@pytest.mark.asyncio
async def test_get_pull_request_review_decision_graphql_error_is_indeterminate(mocker):
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(
            return_value=MockResponse(200, {"errors": [{"message": "unavailable"}]})
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.get_pull_request_review_decision("t", "o", "r", 42)

    assert exc_info.value.status_code == 502

# ─────────────────────────────────────────────────────────────────────────────
# get_pull_request_reviews_checked — pagination
#
# GitHub returns reviews CHRONOLOGICALLY. A single per_page=100 request drops
# everything past the 100th submission — i.e. the MOST RECENT reviews, exactly
# where a CHANGES_REQUESTED would be. rollup_management._pr_closed_is_non_rejection
# uses this as its last live guard when the DB rejection markers are absent.
# ─────────────────────────────────────────────────────────────────────────────


def _review(idx: int, state: str = "COMMENTED") -> dict:
    return {"id": idx, "state": state}


@pytest.mark.asyncio
async def test_reviews_follow_pagination_and_see_late_changes_requested(mocker):
    """A CHANGES_REQUESTED submitted as review #101 MUST be visible."""
    from app import github_pulls

    page1 = [_review(i) for i in range(100)]  # full page → more pages exist
    page2 = [_review(100), _review(101, "CHANGES_REQUESTED")]
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[MockResponse(200, page1), MockResponse(200, page2)]
        ),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 42
    )

    assert len(reviews) == 102
    assert status == github_pulls.REVIEWS_COMPLETE
    assert any(r["state"] == "CHANGES_REQUESTED" for r in reviews), (
        "the late CHANGES_REQUESTED on page 2 was truncated away — a rejected "
        "rollup would be read as a non-rejection close"
    )
    assert mock_request.call_count == 2
    assert mock_request.call_args_list[1].kwargs["params"] == {
        "per_page": 100,
        "page": 2,
    }


@pytest.mark.asyncio
async def test_reviews_short_page_stops_pagination(mocker):
    """A short first page is the last page — no second request."""
    from app import github_pulls

    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, [_review(1)])),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 42
    )

    assert len(reviews) == 1
    assert status == github_pulls.REVIEWS_COMPLETE
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_reviews_keeps_partial_pages_on_mid_pagination_error(mocker):
    """Mirrors get_open_pull_requests: a transient error mid-pagination returns
    the pages already fetched rather than discarding them — and reports TRUNCATED
    so a rejection-deciding caller knows the newest reviews are missing."""
    from app import github_pulls

    page1 = [_review(i, "CHANGES_REQUESTED") for i in range(100)]
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[MockResponse(200, page1), MockResponse(502, text="bad gw")]
        ),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 42
    )

    # The 100 fetched reviews (incl. the rejections) survive the page-2 failure.
    assert len(reviews) == 100
    assert status == github_pulls.REVIEWS_TRUNCATED


@pytest.mark.asyncio
async def test_reviews_401_still_raises(mocker):
    """Auth failure remains a hard 401 — not silently 'no reviews'."""
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(401, {"message": "Bad creds"})),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.get_pull_request_reviews_checked("t", "o", "r", 42)

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# get_pull_request_reviews_checked — TRUNCATED and FAILED are different signals
#
# GitHub serves reviews OLDEST-FIRST, so a TRUNCATED list is missing precisely the
# NEWEST submissions — exactly where a late CHANGES_REQUESTED lives. That is
# EVIDENCE a rejection cannot be ruled out, and a rejection-deciding caller must
# fail closed on it.
#
# A FAILED fetch (page 1 itself errored — rate limit, 502, …) enumerated NOTHING.
# That is mere ABSENCE of evidence, indistinguishable from the 401 path, and
# fail-closing on it would let one transient hiccup flip a routine cleanup-close
# into a terminal FAILED. Collapsing the two onto one bool made a 502 STRICTER
# than a 401, which is backwards.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviews_checked_complete_when_last_page_is_short(mocker):
    from app import github_pulls

    mocker.patch(
        "app.github_pulls._github_request",
        new=AsyncMock(
            side_effect=[
                MockResponse(200, [{"state": "COMMENTED"}] * 100),
                MockResponse(200, [{"state": "APPROVED"}]),
            ]
        ),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 7
    )

    assert len(reviews) == 101
    assert status == github_pulls.REVIEWS_COMPLETE


@pytest.mark.asyncio
async def test_reviews_checked_reports_TRUNCATED_on_mid_pagination_error(mocker):
    """Page 1 succeeded, page 2 failed: we hold real reviews AND know more exist.
    The newest are the missing ones — the caller must be told."""
    from app import github_pulls

    mocker.patch(
        "app.github_pulls._github_request",
        new=AsyncMock(
            side_effect=[
                MockResponse(200, [{"state": "COMMENTED"}] * 100),
                MockResponse(502, {}, text="bad gateway"),
            ]
        ),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 7
    )

    assert len(reviews) == 100, "the pages we did get are still returned"
    assert status == github_pulls.REVIEWS_TRUNCATED


@pytest.mark.asyncio
async def test_reviews_checked_reports_TRUNCATED_at_the_page_cap(mocker):
    """Hitting the 30-page cap is truncation too — more reviews exist unseen."""
    from app import github_pulls

    mocker.patch(
        "app.github_pulls._github_request",
        new=AsyncMock(return_value=MockResponse(200, [{"state": "COMMENTED"}] * 100)),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 7
    )

    assert len(reviews) == 3000
    assert status == github_pulls.REVIEWS_TRUNCATED


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [403, 502, 500])
async def test_reviews_checked_reports_FAILED_when_page_one_errors(mocker, code):
    """A page-1 failure enumerated NOTHING — that is absence of evidence, NOT
    truncation. It must NOT be reported as TRUNCATED, or a single rate-limited
    fetch would fail-close a rejection check and flip a routine cleanup-close into
    a terminal FAILED + operator alert.
    """
    from app import github_pulls

    mocker.patch(
        "app.github_pulls._github_request",
        new=AsyncMock(return_value=MockResponse(code, {}, text="boom")),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 7
    )

    assert reviews == []
    assert status == github_pulls.REVIEWS_FAILED
    assert status != github_pulls.REVIEWS_TRUNCATED, (
        "a fetch failure is not truncation — conflating them makes a 502 stricter "
        "than a 401, which raises and is caught as best-effort"
    )


@pytest.mark.asyncio
async def test_reviews_checked_returns_raw_review_dicts_unwrapped(mocker):
    """The reviews element is a plain list of GitHub's raw review dicts.

    Callers index straight into ``r["state"]``, so the payload must be passed through
    untransformed. (This previously pinned the list-only ``get_pull_request_reviews``
    wrapper; that wrapper was deleted as dead code — it had no callers in ``app/`` and
    its only live effect was to make a stale ``patch("app.github.get_pull_request_reviews")``
    resolve silently, going inert instead of erroring.)
    """
    from app import github_pulls

    mocker.patch(
        "app.github_pulls._github_request",
        new=AsyncMock(return_value=MockResponse(200, [{"state": "APPROVED"}])),
    )

    reviews, status = await github_pulls.get_pull_request_reviews_checked(
        "t", "o", "r", 7
    )

    assert isinstance(reviews, list)
    assert reviews == [{"state": "APPROVED"}]
    assert status == github_pulls.REVIEWS_COMPLETE


# ---------------------------------------------------------------------------
# get_pull_request rate-limit DISCRIMINATOR (N1).
#
# review_loop_convergence's cap-exhaustion path treats a preservability lookup
# FAILURE as "this PR is not salvageable" and CLOSES the PR. So the question
# "is this response a rate limit, or a real error?" is load-bearing: get it
# wrong one way and a drained quota bucket destroys every cap-exhausted rollup
# PR at once; get it wrong the other way and a permanently de-authorized repo
# defers forever. These probe get_pull_request against REAL response shapes —
# the classifier tests in test_review_loop_convergence mock the HTTPException
# directly and so cannot catch a broken discriminator.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pr_empty_bucket_403_is_retryable_503(mocker):
    """Empty-bucket 403 (X-RateLimit-Remaining: 0) -> retryable 503 + Retry-After.

    This is the shape the client SYNTHESISES with zero network when it already
    knows the install-token bucket is drained (github.py ~:471-486), and the shape
    a hard primary rate limit fast-fails with (~:536-540). It must NOT look like a
    hard failure, or the cap path closes a perfectly good PR.
    """
    from app import github_http, github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=_rate_limited_response(reset_in_s=90)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.get_pull_request("ghs_token", "owner", "repo", 42)

    exc = exc_info.value
    assert exc.status_code == 503, (
        "a drained quota bucket was reported as a hard failure — the cap-exhaustion "
        "path reads that as 'PR not salvageable' and CLOSES the PR"
    )
    retry_after = int((exc.headers or {}).get("Retry-After"))
    assert 0 < retry_after <= github_http.RATE_LIMIT_MAX_RETRY_AFTER_S


@pytest.mark.asyncio
async def test_get_pr_secondary_rate_limit_429_without_headers_is_retryable_503(
    mocker,
):
    """A HEADER-LESS 429 (secondary limit) is still a rate limit.

    A hand-rolled "429 AND rate-limit headers" test would miss this and fall
    through to the destructive 500. ``is_rate_limited_response`` treats ANY 429 as
    a rate limit, which is why the canonical helper is used rather than a second
    discriminator.
    """
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(
                429,
                {"message": "You have exceeded a secondary rate limit"},
                text="secondary rate limit",
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.get_pull_request("ghs_token", "owner", "repo", 42)

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_get_pr_permission_403_with_quota_headers_is_NOT_rate_limited(mocker):
    """THE INVERSION GUARD. A PERMISSION 403 must NOT be mistaken for a rate limit.

    GitHub DOES send X-RateLimit-* headers on a permission 403 — but with a
    NONZERO remaining. The discriminator keys on the VALUE (== "0"), not on the
    presence of the header, so this correctly falls through to the hard 500 and
    the cap path keeps its fail-safe (terminal) semantics.

    If this ever flips to 503, a de-authorized repo would DEFER FOREVER instead of
    terminalizing — a permanent wedge.
    """
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(
                403,
                {"message": "Resource not accessible by integration"},
                text="Resource not accessible by integration",
                headers={
                    "X-RateLimit-Remaining": "4999",
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Reset": str(int(time.time()) + 3000),
                },
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.get_pull_request("ghs_token", "owner", "repo", 42)

    assert exc_info.value.status_code == 500, (
        "a PERMISSION 403 was classified as a rate limit — the cap-exhaustion path "
        "would DEFER FOREVER on a condition that never clears"
    )


@pytest.mark.asyncio
async def test_get_pr_404_still_raises_404(mocker):
    """A genuinely-gone PR keeps its own status — never a rate limit."""
    from app import github_pulls

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(404, {}, text="Not Found")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_pulls.get_pull_request("ghs_token", "owner", "repo", 42)

    assert exc_info.value.status_code == 404


# ``pull.base.sha`` is frozen at PR-creation time, but every staleness guard,
# admission check, and claim fence compares reviewer-reported bases (fetched
# live) against it. Once the base branch moved, that mismatch respawned
# exact-target reviews forever (NeurIPS PR #1209, 2026-08-04: one full review
# burned every ~3 minutes). ``get_pull_request`` therefore resolves the live
# branch tip for open PRs.


def _open_pr(base_sha="f" * 40, base_ref="automain", head_sha="a" * 40) -> dict:
    return {
        "number": 42,
        "state": "open",
        "merged": False,
        "base": {"ref": base_ref, "sha": base_sha},
        "head": {"sha": head_sha},
    }


@pytest.mark.asyncio
async def test_get_pull_request_replaces_frozen_base_sha_with_live_tip(mocker):
    from app import github_pulls

    live_tip = "1" * 40
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, _open_pr(base_sha="f" * 40)),
                MockResponse(200, {"object": {"sha": live_tip, "type": "commit"}}),
            ]
        ),
    )

    result = await github_pulls.get_pull_request("ghs_token", "owner", "repo", 42)

    assert result["base"]["sha"] == live_tip
    assert mock_request.call_count == 2
    ref_url = mock_request.call_args_list[1].args[1]
    assert "/git/ref/heads/automain" in ref_url


@pytest.mark.asyncio
async def test_get_pull_request_keeps_snapshot_when_ref_lookup_fails(mocker):
    from app import github_pulls

    frozen = "f" * 40
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            side_effect=[
                MockResponse(200, _open_pr(base_sha=frozen)),
                MockResponse(404, {}, text="Not Found"),
            ]
        ),
    )

    result = await github_pulls.get_pull_request("ghs_token", "owner", "repo", 42)

    assert result["base"]["sha"] == frozen


@pytest.mark.asyncio
async def test_get_pull_request_closed_pr_skips_live_base_lookup(mocker):
    """Closed/merged PRs keep the stored snapshot — the ref may be deleted and
    no staleness decision hangs on them."""
    from app import github_pulls

    frozen = "f" * 40
    closed = _open_pr(base_sha=frozen)
    closed["state"] = "closed"
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, closed)),
    )

    result = await github_pulls.get_pull_request("ghs_token", "owner", "repo", 42)

    assert result["base"]["sha"] == frozen
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_get_pull_request_live_base_opt_out(mocker):
    from app import github_pulls

    frozen = "f" * 40
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, _open_pr(base_sha=frozen))),
    )

    result = await github_pulls.get_pull_request(
        "ghs_token", "owner", "repo", 42, live_base=False
    )

    assert result["base"]["sha"] == frozen
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_get_branch_head_sha_resolves_and_encodes_slash_refs(mocker):
    from app import github_pulls

    tip = "2" * 40
    mock_request = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(200, {"object": {"sha": tip, "type": "commit"}})
        ),
    )

    result = await github_pulls.get_branch_head_sha(
        "ghs_token", "owner", "repo", "research/b7c8df4f"
    )

    assert result == tip
    assert "/git/ref/heads/research%2Fb7c8df4f" in mock_request.call_args.args[1]
