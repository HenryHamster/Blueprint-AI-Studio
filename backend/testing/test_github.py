import hashlib
import hmac
import json
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

    response = await client.post("/api/github/pat", json={"pat": "ghp_classic_token_123"})

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

    response = await client.post("/api/github/pat", json={"pat": "github_pat_valid_token_abc123"})

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

    response = await client.post("/api/github/pat", json={"pat": "github_pat_expired_token_xyz"})

    assert response.status_code == 401


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
        MockResponse(200, {
            "data": {
                "enablePullRequestAutoMerge": {
                    "pullRequest": {
                        "autoMergeRequest": {"enabledAt": "2026-04-15T08:00:00Z"}
                    }
                }
            }
        }),
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
        MockResponse(200, {
            "data": {"enablePullRequestAutoMerge": None},
            "errors": [{"message": "Pull request is in clean status"}],
        }),
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

    result = await enable_auto_merge("token", "owner", "repo", 42, merge_method="INVALID")

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
    _ss.github_quota.update({"remaining": None, "limit": None, "reset_at": None, "last_updated": None})

    async def mock_request(method, *args, **kwargs):
        return MockResponse(200, {"ok": True}, headers={
            "X-RateLimit-Remaining": "3421",
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Reset": "1776400000",
        })

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)

    resp = await _github_request("GET", "https://api.github.com/test", {})
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

    _ss.github_quota.update({"remaining": None, "limit": None, "reset_at": None, "last_updated": None})

    responses = [
        MockResponse(200, {}, headers={"X-RateLimit-Remaining": "500", "X-RateLimit-Reset": "1776400000"}),
        MockResponse(200, {}, headers={"X-RateLimit-Remaining": "50",  "X-RateLimit-Reset": "1776400000"}),
        MockResponse(200, {}, headers={"X-RateLimit-Remaining": "120", "X-RateLimit-Reset": "1776400000"}),
    ]
    it = iter(responses)
    async def mock_request(*args, **kwargs):
        return next(it)

    mocker.patch("httpx.AsyncClient.request", side_effect=mock_request)
    for _ in range(3):
        await _github_request("GET", "https://api.github.com/test", {})
    # Lowest-observed within window wins.
    assert _ss.github_quota["remaining"] == 50

    # After the reset window rolls over, accept the fresh value.
    _ss.github_quota["reset_at"] = 1776400000  # sanity: still the old window
    async def fresh_window(*args, **kwargs):
        return MockResponse(200, {}, headers={"X-RateLimit-Remaining": "4800", "X-RateLimit-Reset": "1776403600"})
    mocker.patch("httpx.AsyncClient.request", side_effect=fresh_window)
    await _github_request("GET", "https://api.github.com/test", {})
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

    resp = await _github_request("GET", "https://api.github.com/test", {}, max_retries=3)
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
    return "sha256=" + hmac.new(
        _TEST_WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()


def _pr_close_payload(owner: str, repo: str, pr_number: int, *, merged: bool, body: str = "") -> bytes:
    """Build a minimal pull_request closed webhook payload."""
    return json.dumps({
        "action": "closed",
        "pull_request": {
            "number": pr_number,
            "merged": merged,
            "body": body,
        },
        "repository": {
            "full_name": f"{owner}/{repo}",
        },
    }).encode()


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

    payload_bytes = _pr_close_payload("testorg", "testrepo", 42, merged=False, body="Not a good fit")
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

    payload_bytes = _pr_close_payload("testorg", "idempotentrepo", 7, merged=False, body="second close")
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

def _make_pr_merged_payload(owner: str, repo: str, pr_number: int, body: str = "") -> bytes:
    """Build a minimal pull_request closed+merged webhook payload."""
    return json.dumps({
        "action": "closed",
        "pull_request": {
            "number": pr_number,
            "merged": True,
            "body": body,
        },
        "repository": {
            "full_name": f"{owner}/{repo}",
        },
    }).encode()


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
    return json.dumps({
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
    }).encode()


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
    return json.dumps({
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
    }).encode()


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
    mocker.patch("app.autonomous_research._check_autonomous_research", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)

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
    mocker.patch("app.autonomous_research._check_autonomous_research", new_callable=AsyncMock)
    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)

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
    mocker.patch("app.autonomous_research._check_autonomous_research", new_callable=AsyncMock)
    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)

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
    mocker.patch("app.autonomous_research._check_autonomous_research", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.notification_dispatch.dispatch_external_notification", new_callable=AsyncMock)

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
async def test_check_run_completed_calls_handle_ci_completion(client, db_session, mocker):
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
async def test_check_run_completed_matches_rollup_parent_by_pr_number(client, db_session, mocker):
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
        return_value={"status": "completed", "conclusion": "success", "failed_logs": ""},
    )
    mock_handle_ci = mocker.patch(
        "app.ci_completion.handle_ci_completion",
        new_callable=AsyncMock,
    )

    project = Project(
        name="Rollup Webhook", user_id="user1",
        github_owner="rollorg", github_repo="rollrepo",
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
    assert (called_task.context or {}).get("review_loop", {}).get("head_sha") == head_sha


@pytest.mark.asyncio
async def test_check_run_completed_pending_checks_skips_ci_completion(client, db_session, mocker):
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
async def test_pr_opened_auto_review_enabled_triggers_review(client, db_session, mocker):
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
async def test_pr_synchronize_auto_review_enabled_triggers_review(client, db_session, mocker):
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

    payload_bytes = _make_pr_event_payload("ardisabledorg", "ardisabledrepo", 5, "opened")
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
