import asyncio
import contextlib
import random
import re as _re
from datetime import datetime
from urllib.parse import quote

import httpx
import logging
import os
import time

# Keep `datetime` re-exportable / referenced so static checkers don't strip it;
# it's used as a forward reference in `get_commit_date(...) -> "datetime | None"`.
_DATETIME = datetime

logger = logging.getLogger(__name__)

GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

# Shared HTTP client with connection pooling — avoids creating a new TCP+TLS
# connection per API call, which causes "All connection attempts failed" under load.
_github_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _github_client
    if _github_client is None or _github_client.is_closed:
        _github_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _github_client


async def close_github_client():
    """Close the shared client. Call from app shutdown/lifespan."""
    global _github_client
    if _github_client and not _github_client.is_closed:
        await _github_client.aclose()
        _github_client = None


def _normalize_endpoint_template(method: str, url: str) -> str:
    """Collapse GitHub URLs to endpoint templates for counting (e.g. /pulls/1852 → /pulls/{n}).

    Prevents counter explosion on PR numbers, SHAs, or branch refs.
    """
    path = url.split("api.github.com", 1)[-1]
    # Strip query string; it's not useful for grouping hotspots.
    path = path.split("?", 1)[0]
    # Collapse /repos/{owner}/{repo}/
    path = _re.sub(r"^/repos/[^/]+/[^/]+", "/repos/{owner}/{repo}", path)
    # Collapse /app/installations/{id}/
    path = _re.sub(r"^/app/installations/\d+", "/app/installations/{id}", path)
    # Numeric IDs (PR numbers, issue numbers, check-run IDs)
    path = _re.sub(r"/\d+(?=/|$)", "/{n}", path)
    # 40-char SHA or 7+ hex
    path = _re.sub(r"/[a-f0-9]{7,40}(?=/|$)", "/{sha}", path)
    # git refs like /git/ref/heads/anything
    path = _re.sub(r"/(heads|refs|tags)/[^/]+", r"/\1/{ref}", path)
    return f"{method} {path}"


def _record_call(method: str, url: str) -> None:
    """Bump the per-endpoint counter so we can see which endpoints drain
    the 5000/hr quota when the window runs low."""
    import app.shared_state as _ss

    template = _normalize_endpoint_template(method, url)
    _ss.github_call_counts[template] = _ss.github_call_counts.get(template, 0) + 1


async def probe_rate_limit() -> bool:
    """Ping /rate_limit (free call) to keep github_quota fresh for the dashboard.

    Uses cached install token (not JWT) — install bucket is 5000/hr vs JWT's ~60/hr,
    so probing with JWT would make quota look exhausted when the install bucket is fine.
    Returns True on success, False if no cached token (pill waits for organic traffic).
    """
    # Pull any currently-cached installation token — doesn't spend quota
    # on a refresh.  If nothing's cached, skip the probe; real activity
    # will populate the pill soon enough.
    from app.github_token_resolver import _github_token_cache

    cached_token: str | None = None
    for entry in _github_token_cache.values():
        token = entry.get("token")
        if token and token.startswith(("ghs_", "ghu_")):
            cached_token = token
            break
    if not cached_token:
        return False
    headers = {
        "Authorization": f"Bearer {cached_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = await _github_request(
            "GET",
            "https://api.github.com/rate_limit",
            headers,
            max_retries=1,
        )
        return resp is not None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[GITHUB] probe_rate_limit failed: {e}")
        return False


# Fail-fast threshold for the retry loop: if a rate-limited response's reset
# window is further out than this, return the response immediately instead of
# sleeping and retrying.  Caller sees the 403 and can decide; no amplified
# counter increment, no blocked worker, no JWT bucket re-drained.
RATE_LIMIT_FAST_FAIL_THRESHOLD_S = 5


def _relevant_bucket(url: str, headers: dict | None) -> dict:
    """Identify which shared_state bucket a request belongs to.

    Install-token (ghs_/ghu_) → github_quota (5000/hr).
    App JWT (Bearer eyJ...) or /app/* URL → github_jwt_quota (~60/hr).
    Everything else (unauthenticated, PAT) → github_quota as the default; we
    only *gate* on values we actually observed, so an unused bucket won't cause
    spurious short-circuits.
    """
    import app.shared_state as _ss

    auth = (headers or {}).get("Authorization", "")
    if "/app/" in url or auth.startswith("Bearer eyJ"):
        return _ss.github_jwt_quota
    return _ss.github_quota


def _update_quota_state(
    remaining: int,
    limit: int | None,
    reset_ts: int | None,
    *,
    bucket_name: str = "install",
) -> None:
    """Mirror X-RateLimit-* headers into the correct shared_state quota bucket.

    `bucket_name` ∈ {"install", "jwt"} — selects `github_quota` or
    `github_jwt_quota`.  Previously only the install-token bucket was tracked;
    splitting lets the JWT bucket get its own pre-call short-circuit and lets
    the dashboard pill stay scoped to install-token.

    Keeps the LOWEST `remaining` seen before the reset time (so a later
    higher value doesn't mask a recent crisis until the window resets).
    Resets the per-endpoint call counter on each install-bucket quota-window
    rollover so the counts always describe the current 1-hour window; the JWT
    bucket is tracked independently and does not affect the counter.
    """
    from datetime import datetime, timezone
    import app.shared_state as _ss

    q = _ss.github_jwt_quota if bucket_name == "jwt" else _ss.github_quota
    now_iso = datetime.now(timezone.utc).isoformat()
    prev_reset = q.get("reset_at")
    prev_remaining = q.get("remaining")
    prev_limit = q.get("limit")
    # Accept the new observation unconditionally (treating this like a
    # rollover) when any of these are true:
    #   - reset_ts advanced (natural hourly rollover)
    #   - limit changed (we're now tracking a different bucket type —
    #     e.g. install-token 5000 after previously seeing App-JWT 60 —
    #     the prior remaining value is meaningless for the new bucket)
    #   - reset_ts moved backward (shouldn't normally happen, but if it
    #     does we definitely aren't in the same window anymore)
    bucket_switched = (
        prev_limit is not None and limit is not None and prev_limit != limit
    )
    rolled_over = (
        prev_reset is not None and reset_ts is not None and reset_ts != prev_reset
    )
    accept_unconditional = bucket_switched or rolled_over
    if accept_unconditional:
        # Endpoint counter reset only makes sense for the install bucket — the
        # counter scopes to "this 5000/hr window", not to JWT activity.
        if bucket_name == "install":
            _ss.github_call_counts.clear()
            for k in _ss.github_cache_hits:
                _ss.github_cache_hits[k] = 0
            if reset_ts is not None:
                _ss.github_call_counts_window_reset_at = reset_ts
        q["remaining"] = remaining
    elif prev_remaining is None or remaining < prev_remaining:
        q["remaining"] = remaining
    if limit is not None:
        q["limit"] = limit
    if reset_ts is not None:
        q["reset_at"] = reset_ts
        if bucket_name == "install" and _ss.github_call_counts_window_reset_at is None:
            _ss.github_call_counts_window_reset_at = reset_ts
    q["last_updated"] = now_iso


def _synthetic_rate_limited_response(bucket: dict) -> httpx.Response:
    """Return a 403 rate-limited response without making a network call.

    Used when we already know the relevant bucket is empty — keeps the caller's
    error-handling path identical while avoiding the wasted retry and the
    amplified per-endpoint counter increment.
    """
    reset = bucket.get("reset_at") or int(time.time()) + 60
    headers = {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Limit": str(bucket.get("limit") or 0),
        "X-RateLimit-Reset": str(reset),
        "Content-Type": "application/json",
    }
    return httpx.Response(
        status_code=403,
        headers=headers,
        content=b'{"message":"rate limit exceeded (short-circuited locally)"}',
        request=None,
    )


async def _github_request(
    method: str,
    url: str,
    headers: dict,
    max_retries: int = 3,
    project_id: str | None = None,
    **kwargs,
) -> httpx.Response:
    """Make a GitHub API request with retry on 5xx/network/rate-limit errors."""
    response = None
    # Pre-call bucket check: if we've recently observed the relevant bucket at
    # zero with a reset more than RATE_LIMIT_FAST_FAIL_THRESHOLD_S away, don't
    # bother sending — the response will be a 403 anyway, which would count
    # against the per-endpoint counter and trigger downstream retry loops.
    bucket = _relevant_bucket(url, headers)
    b_remaining = bucket.get("remaining")
    b_reset = bucket.get("reset_at")
    if (
        b_remaining == 0
        and b_reset is not None
        and (b_reset - int(time.time())) > RATE_LIMIT_FAST_FAIL_THRESHOLD_S
    ):
        import app.shared_state as _ss

        label = "JWT" if bucket is _ss.github_jwt_quota else "install-token"
        _ss.github_cache_hits["bucket_short_circuit"] = (
            _ss.github_cache_hits.get("bucket_short_circuit", 0) + 1
        )
        logger.warning(
            f"[GITHUB] {method} {url} short-circuited — {label} bucket empty, "
            f"resets in {b_reset - int(time.time())}s"
        )
        return _synthetic_rate_limited_response(bucket)
    for attempt in range(max_retries):
        try:
            client = _get_client()
            # Count each attempt against the per-endpoint counter BEFORE the
            # call — even a 403 rate-limit response counts as "we tried this
            # endpoint" which is what we care about when diagnosing burn
            # (though per GitHub docs 403s don't consume quota themselves).
            _record_call(method, url)
            response = await client.request(method, url, headers=headers, **kwargs)
            # 429 or 403 rate limit — GitHub uses 403 for primary rate limit
            # exhaustion (5,000 req/hr) and 429 for secondary/search limits.
            # Secondary abuse limits return 403 + Retry-After WITHOUT
            # X-RateLimit-Remaining: 0, so also treat 403 + Retry-After as retriable.
            is_rate_limited = response.status_code == 429 or (
                response.status_code == 403
                and (
                    response.headers.get("X-RateLimit-Remaining") == "0"
                    or response.headers.get("Retry-After") is not None
                )
            )
            if is_rate_limited and attempt < max_retries - 1:
                retry_after = response.headers.get("Retry-After")
                reset_ts = response.headers.get("X-RateLimit-Reset")
                if retry_after:
                    delay = min(int(retry_after), 120)
                elif reset_ts:
                    delay = min(max(0, int(reset_ts) - int(time.time())) + 1, 120)
                else:
                    delay = min(2**attempt * 10, 120)
                # Fail fast on "hard" rate limits: if the reset window is more
                # than a few seconds out, retrying is guaranteed to burn extra
                # 403s against the counter without changing the outcome. Return
                # the response; the caller can decide whether to retry later.
                if delay > RATE_LIMIT_FAST_FAIL_THRESHOLD_S:
                    logger.warning(
                        f"[GITHUB] {method} {url} rate limited ({response.status_code}); "
                        f"reset in {delay}s — failing fast (no retry)"
                    )
                    return response
                logger.warning(
                    f"[GITHUB] {method} {url} rate limited ({response.status_code}), "
                    f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue
            # Proactive rate limit check — back off before hitting the limit.
            # Only delay on non-success responses (retries); never block a
            # successful response — the caller may have a short timeout.
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining is not None and response.status_code >= 400:
                try:
                    if int(remaining) < 10:
                        reset_ts = response.headers.get("X-RateLimit-Reset")
                        if reset_ts:
                            wait = max(0, int(reset_ts) - int(time.time())) + 1
                            logger.warning(
                                f"[GITHUB] Rate limit critical: {remaining} calls left, waiting {wait}s until reset"
                            )
                            await asyncio.sleep(min(wait, 120))  # cap at 2min
                except (ValueError, TypeError):
                    pass  # Malformed header, skip rate limit check
            # Mirror rate-limit headers into shared_state so /api/health/detailed
            # and the dashboard widget can show remaining quota + reset time.
            # GitHub has SEPARATE rate-limit buckets per token type:
            #   - Installation token (ghs_... or ghu_...): 5000/hr → github_quota
            #   - App JWT (eyJ...) / /app/* endpoints: ~60/hr → github_jwt_quota
            #   - PAT / user token: 5000/hr → github_quota (treated as install)
            #   - Unauthenticated: 60/hr → not tracked (would pollute)
            if remaining is not None:
                _auth = headers.get("Authorization", "") if headers else ""
                _is_install_token = "Bearer ghs_" in _auth or "Bearer ghu_" in _auth
                _is_jwt = "/app/" in url or _auth.startswith("Bearer eyJ")
                _bucket_name: str | None
                if _is_install_token:
                    _bucket_name = "install"
                elif _is_jwt:
                    _bucket_name = "jwt"
                else:
                    _bucket_name = None  # unauthenticated / unknown — don't track
                if _bucket_name is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        _update_quota_state(
                            remaining=int(remaining),
                            limit=int(response.headers.get("X-RateLimit-Limit", 0))
                            or None,
                            reset_ts=int(response.headers.get("X-RateLimit-Reset", 0))
                            or None,
                            bucket_name=_bucket_name,
                        )
            if response.status_code == 401:
                logger.warning(
                    f"[GITHUB] {method} {url} returned 401 Unauthorized — "
                    f"token may be expired or revoked"
                )
                if project_id:
                    from app.github_token_resolver import (
                        invalidate_github_token_cache,
                    )

                    invalidate_github_token_cache(project_id)
                    logger.info(
                        f"[GITHUB] Invalidated token cache for project {project_id} after 401"
                    )
            if response.status_code < 500 or attempt == max_retries - 1:
                if attempt > 0 and response.status_code >= 500:
                    logger.error(
                        f"[GITHUB] {method} {url} failed with {response.status_code} "
                        f"after {max_retries} attempts"
                    )
                return response
            delay = 2**attempt * (0.5 + random.random() * 0.5)  # jittered: 0.5x-1x base
            logger.warning(
                f"[GITHUB] {method} {url} returned {response.status_code}, "
                f"retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(delay)
        except httpx.HTTPError as exc:
            if attempt == max_retries - 1:
                raise
            # Transport-layer failures (connect/read/pool) often mean a
            # keep-alive in the shared pool is dead. Close the client so the
            # next attempt rebuilds TCP+TLS instead of reusing the broken
            # connection. Non-transport HTTPErrors (timeouts on a fresh conn,
            # invalid URL, etc.) don't benefit from a pool reset.
            if isinstance(
                exc,
                (
                    httpx.ConnectError,
                    httpx.ReadError,
                    httpx.WriteError,
                    httpx.PoolTimeout,
                ),
            ):
                await close_github_client()
                logger.warning(
                    f"[GITHUB] Reset HTTP client after transport error: {type(exc).__name__}"
                )
            delay = 2**attempt * (0.5 + random.random() * 0.5)  # jittered: 0.5x-1x base
            logger.warning(
                f"[GITHUB] {method} {url} network error: {exc}, "
                f"retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(delay)
    assert response is not None  # loop always runs at least once (max_retries >= 1)
    return response  # unreachable but satisfies type checker


async def get_file_content_at_ref(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
) -> tuple[bool | None, int | None]:
    """Fetch *path* at ``owner/repo@ref`` via the GitHub contents API.

    The GitHub-layer foundation for branch-aware grounding: line-number
    validation needs file CONTENT, but the existing existence helpers only
    answer "is this path here?". Goes through :func:`_github_request`, so it is
    quota-accounted and rate-limit-aware like every other GitHub call, and is
    reused by the source-ref, symbol-grounding, and playbook-grounding paths.

    Returns ``(exists, line_count)`` with a tri-state ``exists``:

      * ``True``  — the path is a file on ``ref``; ``line_count`` is its
        newline count (matching ``count("\\n")``), or ``None`` when the body
        could not be decoded.
      * ``False`` — ``ref`` unambiguously has no such file (HTTP 404).
      * ``None``  — indeterminate (non-200/non-404 status, a non-file payload,
        an undecodable body, or any error). Callers MUST NOT treat ``None`` as
        absence — only ``False`` is a definite absence on the branch.

    ``line_count`` is ``None`` whenever it cannot be determined, so an
    indeterminate count never invalidates a line-number citation.
    """
    import base64

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    encoded_path = quote(path, safe="/")
    encoded_ref = quote(ref, safe="")
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{encoded_path}?ref={encoded_ref}"
    )
    try:
        response = await _github_request(
            "GET", url, headers, max_retries=2, project_id=project_id
        )
        if response.status_code == 404:
            return False, None
        if response.status_code != 200:
            logger.warning(
                "[GITHUB] contents fetch for %s on %s/%s@%s: HTTP %s",
                path,
                owner,
                repo,
                ref,
                response.status_code,
            )
            return None, None
        payload = response.json()
        if not (isinstance(payload, dict) and payload.get("type") == "file"):
            return None, None
        content_b64 = payload.get("content")
        if not isinstance(content_b64, str):
            return True, None
        try:
            text = base64.b64decode(content_b64).decode(errors="replace")
        except Exception:  # noqa: BLE001 — malformed base64 → unknown line count
            return True, None
        return True, text.count("\n")
    except Exception as e:  # noqa: BLE001 — never let a fetch error read as absence
        logger.warning(
            "[GITHUB] Failed contents fetch for %s on %s/%s@%s: %s",
            path,
            owner,
            repo,
            ref,
            e,
        )
        return None, None


# ---------------------------------------------------------------------------
# Public-interface façade
#
# github.py was decomposed into single-responsibility sibling modules to keep
# each concern small and to stop parallel edits from clashing on one 2.9k-line
# file.  The low-level request dispatcher and rate-limit/quota state above are
# the shared core that those modules build on.  Everything else now lives in
# app/github_<concern>.py and is re-exported here so that every existing
# ``from app.github import ...`` and ``github.<symbol>`` call site keeps working
# with no edits, and so monkeypatching against ``app.github.<symbol>`` (the
# canonical patch namespace across the test suite) stays effective.
#
# The re-exported names below are bound to the SAME function/object the
# defining submodule holds, so patching ``app.github.X`` and clearing the
# shared cache dicts (``_check_runs_cache`` / ``_installation_id_cache``)
# behave exactly as before the split.
# ---------------------------------------------------------------------------

from app.github_auth import (  # noqa: F401
    generate_github_app_jwt,
    get_repo_installation_token,
    exchange_oauth_code,
    get_app_info,
    get_user_installations,
    get_github_user,
    generate_installation_access_token,
    validate_fine_grained_pat,
    _INSTALLATION_ID_TTL_SECONDS,
    _installation_id_cache,
)
from app.github_checks import (  # noqa: F401
    update_commit_status,
    create_check_run,
    update_check_run,
    _unique,
    get_check_runs,
    invalidate_check_runs_cache,
    get_check_runs_cached,
    _CHECK_RUNS_CACHE_TTL_S,
    _CHECK_RUNS_CACHE_MAX,
    _check_runs_cache,
)
from app.github_issues import (  # noqa: F401
    create_pr_comment,
    add_issue_label,
    create_pr_review_comment,
    submit_pr_review,
    create_issue,
    get_issues,
    get_single_issue,
    get_issue_comments,
    create_issue_comment,
)
from app.github_refs import (  # noqa: F401
    create_branch,
    create_tag,
    get_branch_sha,
    get_repo_default_branch,
    delete_branch,
    list_branches,
    get_commit_date,
    list_pull_requests_for_commit,
    is_ancestor_of,
    get_files_changed_between_refs,
)
from app.github_pulls import (  # noqa: F401
    pr_is_not_behind,
    pr_is_not_behind_for,
    create_pull_request,
    get_pull_request,
    get_pull_request_reviews,
    get_open_pull_requests,
    get_pull_request_diff,
    get_pull_request_commits,
    merge_pull_request,
    get_pull_request_merge_state,
    enable_auto_merge,
    update_branch,
    update_branch_with_sha_retry,
    close_pull_request,
    reopen_pull_request,
    get_pr_files,
    _UPDATE_BRANCH_NO_NEW_COMMITS_PHRASE,
    _is_no_new_commits_message,
    _tag_not_behind_if_no_new_commits,
)
from app.github_merge_summary import (  # noqa: F401
    build_merge_summary_body,
    post_merge_summary_comment,
    build_blueprint_pr_merge_summary_comment,
    post_pipeline_merge_summary_comment,
    BLUEPRINT_PR_MERGE_SUMMARY_MARKER,
)
