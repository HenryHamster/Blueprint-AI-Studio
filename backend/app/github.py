"""GitHub API client and rate-limit tracking.

Provides a pooled async HTTP client for GitHub API operations (pull requests,
commits, branches, checks, etc.) with automatic JWT and installation-token auth,
rate-limit tracking across two buckets (install-token 5000/hr, app-JWT ~60/hr),
and fail-fast backoff when rate limits are exhausted. Tracks API call volumes
per endpoint (POST /repos/{{owner}}/{{repo}}/pulls, GET /repos/{{owner}}/{{repo}}/commits/{sha},
etc.) to surface quota consumption hot spots on the admin dashboard.

Functions are designed for use in the orchestrator, agent, and background workers.
Client lifecycle is managed by app.lifecycle (init on startup, close on shutdown).
"""

import asyncio
import contextlib
import hashlib
import random
import re as _re
from datetime import datetime
from urllib.parse import quote, urlencode
from collections import OrderedDict

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

# Single-flight read coalescing: concurrent bodyless GETs to the identical URL
# share ONE in-flight request (keyed by URL and a one-way credential identity)
# instead of each hitting
# GitHub. Safe for every read — a coalesced waiter never sees anything staler
# than a live request would. Collapses the ~50-VM fan-out that hammers a shared
# ref (e.g. every rollup parent reading automain's tip in the same sweep), a
# dominant install-token-budget drain.
_inflight_reads: dict[
    tuple[str, str, str, str, str, str, str, str, str],
    "asyncio.Future[httpx.Response]",
] = {}

# File-content cache: memoizes get_file_text_at_ref results by
# (owner, repo, ref, path) for a short TTL, but only when ``ref`` is a full
# commit SHA. Branch names are moving refs: caching ``main`` here made callers
# that explicitly bypassed their own higher-level cache still observe an old
# project manifest after a merge. At an immutable commit SHA, caching avoids
# redundant GitHub API content reads for the same file — the dominant cost in
# ci-compliance evaluation (~213 capped files per new integration-branch SHA).
# Bounded LRU eviction prevents unbounded growth under sustained rollup activity.
_FILE_CONTENT_CACHE_TTL_SECONDS = 600  # 10 minutes
_FILE_CONTENT_CACHE_MAX_SIZE = 2000   # ~2000 entries ≈ ~3-5 MB at average file size
_file_content_cache: "OrderedDict[tuple[str, str, str, str], tuple[str, float]]" = OrderedDict()
_FULL_COMMIT_SHA_RE = _re.compile(r"^[0-9a-fA-F]{40}$")


class StaleBranchHeadError(RuntimeError):
    """A compare-and-swap branch write lost its lease to a newer tip."""


def _get_client() -> httpx.AsyncClient:
    global _github_client
    if _github_client is None or _github_client.is_closed:
        _github_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
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


def _record_consumer_call(consumer: str | None, url: str) -> None:
    """Bump the per-consumer counter alongside _record_call.

    ``consumer`` names the high-level caller (e.g. "release_qualifier",
    "ci_monitor") so post-incident analysis can see WHICH loop/sweep drained
    the install-token budget.  Falls back to "unknown" when no tag is supplied.
    Only counts install-token paths (same scope as github_call_counts).
    """
    import app.shared_state as _ss

    key = consumer or "unknown"
    _ss.github_consumer_counts[key] = _ss.github_consumer_counts.get(key, 0) + 1


async def probe_rate_limit() -> bool:
    """Ping /rate_limit (free call) to keep github_quota fresh for the dashboard.

    Uses cached install token (not JWT) — install bucket is 5000/hr vs JWT's ~60/hr,
    so probing with JWT would make quota look exhausted when the install bucket is fine.
    Returns True on success, False if no cached token (pill waits for organic traffic).
    """
    # Pull any currently-cached installation token — doesn't spend quota
    # on a refresh.  If nothing's cached, skip the probe; real activity
    # will populate the pill soon enough.
    #
    # github_auth's per-repo cache is the authoritative home for App-discovery
    # tokens (the common case). The resolver's per-project cache only holds
    # tokens IT minted (stored-installation fallback), so check both — checking
    # only the latter would leave the quota pill dark on most deployments.
    from app.github_auth import any_cached_installation_token
    from app.github_token_resolver import (
        _github_token_cache,
        _TOKEN_MIN_REMAINING_LIFE,
        _remaining_life,
    )

    cached_token: str | None = any_cached_installation_token()
    if not cached_token:
        for entry in _github_token_cache.values():
            token = entry.get("token")
            if (
                token
                and token.startswith(("ghs_", "ghu_"))
                and _remaining_life(entry) >= _TOKEN_MIN_REMAINING_LIFE
            ):
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

# Alert once per hourly window when the install-token bucket first drops to this
# fraction remaining — early warning before the bucket empties and stalls the
# pipeline. Env-overridable to match the pipeline-health threshold style.
GITHUB_BUDGET_ALERT_FRACTION = float(os.getenv("GITHUB_BUDGET_ALERT_FRACTION", "0.20"))
_last_budget_alert_window: int | None = None

# Deferrable consumers (CI failure-detail fan-out, workflow-run diagnostics) skip
# their expensive multi-call fan-out when the install bucket falls to this floor.
# Lower than GROUNDING_QUOTA_RESERVE (500) — diagnostics are less critical than
# existence checks, but still leaves headroom for promotion/merge ops.
DEFERRABLE_QUOTA_FLOOR = 300


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
            # Snapshot consumer counts before clearing so the previous window's
            # breakdown survives for post-incident inspection.
            if _ss.github_consumer_counts:
                _ss.github_consumer_counts_snapshot = dict(_ss.github_consumer_counts)
                _ss.github_consumer_counts.clear()
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

    # Accumulate off-backend burn estimate for the install bucket.
    # Compares observed X-RateLimit-Remaining drop to locally-counted backend
    # calls; the gap represents external consumption (agent VMs, CI scripts).
    if bucket_name == "install":
        est = _ss.github_off_backend_estimate
        if accept_unconditional:
            # Rollover: github_call_counts was just cleared above, so anchor to 0.
            est["cumulative_this_window"] = 0
            est["_last_remaining"] = remaining
            est["_last_backend_calls"] = sum(_ss.github_call_counts.values())
        elif est.get("_last_remaining") is not None:
            current_backend_calls = sum(_ss.github_call_counts.values())
            observed_drop = est["_last_remaining"] - remaining
            local_delta = current_backend_calls - est["_last_backend_calls"]
            drift = max(0, observed_drop - local_delta)
            est["cumulative_this_window"] = est.get("cumulative_this_window", 0) + drift
            est["_last_remaining"] = remaining
            est["_last_backend_calls"] = current_backend_calls
        else:
            # First observation this window — establish anchor, nothing to diff.
            est["_last_remaining"] = remaining
            est["_last_backend_calls"] = sum(_ss.github_call_counts.values())

    # Budget alert: warn ONCE per hourly window when the install bucket first
    # crosses <=20% remaining, so an operator sees exhaustion coming instead of
    # discovering it after the pipeline has already stalled. Window-keyed dedup
    # (on reset_ts) is self-clearing — a new window can alert again.
    if (
        bucket_name == "install"
        and limit
        and remaining is not None
        and reset_ts is not None
    ):
        global _last_budget_alert_window
        if (
            remaining <= limit * GITHUB_BUDGET_ALERT_FRACTION
            and reset_ts != _last_budget_alert_window
        ):
            _last_budget_alert_window = reset_ts
            _post_github_budget_alert(remaining, limit, reset_ts)


def _post_github_budget_alert(remaining: int, limit: int, reset_ts: int) -> None:
    """Emit a low-budget alert (structured log + fire-and-forget webhook).

    The install token is a system-global resource (no project scope) and this
    runs on the sync request hot path with no DB session, so this mirrors
    ``pipeline_health_state._post_pipeline_alert`` — a structured ``alert`` log
    plus a detached webhook — rather than a project-scoped ``Alert`` DB row.
    All dispatch errors are swallowed; a failed alert must never break a request.
    """
    import app.shared_state as _ss

    # Snapshot consumer counts at alert time so the post-incident breakdown
    # captures who was burning quota at the moment the low-budget threshold
    # fired. Unlike rollover, we do NOT clear the live dict here — the window
    # is still active and counts keep accumulating.
    if _ss.github_consumer_counts:
        _ss.github_consumer_counts_snapshot = dict(_ss.github_consumer_counts)

    pct = (100 * remaining / limit) if limit else 0
    title = "GitHub install-token budget low"
    _backend_calls = sum(_ss.github_call_counts.values())
    _ext_estimate = _ss.github_off_backend_estimate.get("cumulative_this_window", 0)
    message = (
        f"{remaining}/{limit} ({pct:.0f}%) install-token budget remaining; "
        f"resets in {max(0, reset_ts - int(time.time()))}s. "
        f"Backend calls this window: {_backend_calls}; "
        f"estimated off-backend burn: {_ext_estimate}. "
        f"Top consumers: see github_consumer_counts_snapshot on /api/health/detailed."
    )
    logger.error(
        title,
        extra={
            "alert": True,
            "type": "github_budget",
            "remaining": remaining,
            "limit": limit,
            "reset_at": reset_ts,
        },
    )
    try:
        from app.notification import _send_webhook_notification

        # Detached — webhook is fire-and-forget; RuntimeError => no running loop
        # (sync test), which correctly skips dispatch (log-only).
        loop = asyncio.get_running_loop()
        loop.create_task(  # noqa: RUF006
            _send_webhook_notification("GITHUB_BUDGET_LOW", title, message)
        )
    except RuntimeError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[GITHUB] budget alert webhook dispatch skipped: {e}")


def _deferrable_quota_active() -> bool:
    """Whether deferrable consumers should skip diagnostic fan-out.

    True only when the install bucket has been observed below DEFERRABLE_QUOTA_FLOOR
    with a reset still in the future. An unobserved bucket (remaining is None)
    never gates — only gate on observed evidence, not boot-time unknowns.
    """
    import app.shared_state as _ss

    q = _ss.github_quota
    remaining = q.get("remaining")
    if remaining is None or remaining >= DEFERRABLE_QUOTA_FLOOR:
        return False
    reset_at = q.get("reset_at")
    # Window already rolled over; stale low reading is meaningless.
    return reset_at is None or int(reset_at) > int(time.time())


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


def is_github_rate_limited_response(response: httpx.Response) -> bool:
    """Recognize GitHub primary/secondary limits without conflating permission 403s."""

    return response.status_code == 429 or (
        response.status_code == 403
        and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or response.headers.get("Retry-After") is not None
        )
    )


def _github_auth_fingerprint(headers: dict | None) -> str:
    """Return a non-reversible single-flight identity; never retain the credential."""

    authorization = httpx.Headers(headers or {}).get("authorization", "")
    return hashlib.sha256(authorization.encode("utf-8")).hexdigest()


def _response_header_fingerprint(headers: dict | None) -> tuple[str, str]:
    normalized = httpx.Headers(headers or {})
    return (
        normalized.get("accept", ""),
        normalized.get("x-github-api-version", ""),
    )


def _canonical_query_params(params) -> str:
    """Return a stable, credential-free representation of request params.

    ``httpx`` accepts mappings, ``QueryParams``, and repeated key/value pairs.
    Sorting the encoded pairs makes equivalent mappings coalesce while keeping
    page, head, and other semantically distinct reads separate.
    """
    if params is None:
        return ""
    try:
        # QueryParams is the one canonical parser for mappings, repeated pairs,
        # and already-encoded query values.  In particular, it avoids treating
        # a list value as one Python string and avoids collisions between
        # ``[("label", "a"), ("label", "b")]`` and ``{"label": ["a", "b"]}``.
        items = list(httpx.QueryParams(params).multi_items())
    except (TypeError, ValueError):
        return repr(params)
    normalized = [(str(key), str(value)) for key, value in items]
    normalized.sort()
    return urlencode(normalized, doseq=True)


async def _github_request(
    method: str,
    url: str,
    headers: dict,
    max_retries: int = 3,
    project_id: str | None = None,
    consumer: str | None = None,
    **kwargs,
) -> httpx.Response:
    """Coalescing dispatcher in front of :func:`_github_request_uncoalesced`.

    Bodyless GETs (side-effect-free reads) to the identical URL collapse to ONE
    in-flight request via ``_inflight_reads``; every other method — and any GET
    carrying a body (json/data/content) — runs uncoalesced. The get-or-create of
    the in-flight entry contains NO ``await``, so on the single-threaded event
    loop two concurrent callers can never both launch the same read (the load-
    bearing invariant). The shared task is detached (``ensure_future``), so a
    caller cancelled by its own timeout does not cancel the request other waiters
    depend on; the done-callback pops the key on success AND failure, so a failed
    read is never memoized past its in-flight window.
    """
    if (
        method.upper() != "GET"
        or "json" in kwargs
        or "data" in kwargs
        or "content" in kwargs
    ):
        return await _github_request_uncoalesced(
            method, url, headers, max_retries, project_id, consumer=consumer, **kwargs
        )
    # The same URL can return different repository sets and permissions for two
    # user/App credentials. Coalesce only within one credential identity.
    accept, api_version = _response_header_fingerprint(headers)
    key = (
        "GET",
        url,
        _github_auth_fingerprint(headers),
        _canonical_query_params(kwargs.get("params")),
        accept,
        api_version,
        project_id or "",
        consumer or "",
        str(max(1, int(max_retries))),
    )
    existing = _inflight_reads.get(key)
    if existing is not None:
        import app.shared_state as _ss

        _ss.github_cache_hits["read_singleflight"] = (
            _ss.github_cache_hits.get("read_singleflight", 0) + 1
        )
        return await asyncio.shield(existing)
    task = asyncio.ensure_future(
        _github_request_uncoalesced(
            method, url, headers, max_retries, project_id, consumer=consumer, **kwargs
        )
    )
    _inflight_reads[key] = task
    def _forget_completed(done_task: asyncio.Future) -> None:
        if _inflight_reads.get(key) is done_task:
            _inflight_reads.pop(key, None)

    task.add_done_callback(_forget_completed)
    return await asyncio.shield(task)


async def _github_request_uncoalesced(
    method: str,
    url: str,
    headers: dict,
    max_retries: int = 3,
    project_id: str | None = None,
    consumer: str | None = None,
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
            _record_consumer_call(consumer, url)
            response = await client.request(method, url, headers=headers, **kwargs)
            # 429 or 403 rate limit — GitHub uses 403 for primary rate limit
            # exhaustion (5,000 req/hr) and 429 for secondary/search limits.
            # Secondary abuse limits return 403 + Retry-After WITHOUT
            # X-RateLimit-Remaining: 0, so also treat 403 + Retry-After as retriable.
            is_rate_limited = is_github_rate_limited_response(response)
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
                # Also drop any repo-scoped install token cached by
                # get_repo_installation_token (that path has no project_id).
                # Parse owner/repo from the repo API URL so a revoked token
                # self-heals on the next call instead of 401-ing for ~50 min.
                _repo_m = _re.search(
                    r"api\.github\.com/repos/([^/?#]+)/([^/?#]+)", url
                )
                if _repo_m:
                    from app.github_auth import (
                        invalidate_repo_installation_token_cache,
                        invalidate_repo_installation_read_token_cache,
                    )

                    invalidate_repo_installation_token_cache(
                        _repo_m.group(1), _repo_m.group(2)
                    )
                    invalidate_repo_installation_read_token_cache(
                        _repo_m.group(1), _repo_m.group(2)
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


async def _list_versions_dir_entries(
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
    versions_dir: str = "backend/alembic/versions",
) -> list[dict] | None:
    """Fetch a migration versions directory listing at *ref*.

    Returns the list of file entries (each a GitHub contents dict carrying at
    least ``name`` and its git blob ``sha``) on HTTP 200, or ``None`` when the
    directory does not exist (HTTP 404 — a greenfield repo with no migrations).
    RAISES on any other non-200 status, non-list payload, or transport error so
    callers can defer (never wave a merge/qualification through on an unreadable
    target). Shared by :func:`list_versions_at_ref` and
    :func:`list_version_shas_at_ref` so both go through one request and one
    fail-safe contract.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    versions_dir = versions_dir.strip("/")
    encoded_path = quote(versions_dir, safe="/")
    encoded_ref = quote(ref, safe="")
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{encoded_path}?ref={encoded_ref}"
    )
    response = await _github_request(
        "GET", url, headers, max_retries=2, project_id=project_id
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(
            f"GitHub contents listing for {versions_dir} on "
            f"{owner}/{repo}@{ref} returned HTTP {response.status_code}"
        )
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(
            f"GitHub contents listing for {versions_dir} on "
            f"{owner}/{repo}@{ref} did not return a directory listing"
        )
    return [
        entry
        for entry in payload
        if isinstance(entry, dict)
        and entry.get("type") == "file"
        and isinstance(entry.get("name"), str)
    ]


async def list_versions_at_ref(
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
    versions_dir: str = "backend/alembic/versions",
) -> list[str]:
    """List Alembic migration filenames under *versions_dir* at *ref*.

    Supplies the rollup migration guard with the *target branch's current*
    migration set so the combined chain (target head + the rollup's added
    migrations) can be validated. Returns the basenames of the files in the
    versions directory (e.g. ``["0001_init.py", ..., "0137_cascade.py"]``);
    callers derive the existing revision ids and the current head from those
    filenames (the filename ``NNNN`` prefix is kept in lockstep with the
    in-file revision by the migration-chain invariant).

    Fail-safe contract for the auto-merge guard: returns ``[]`` when the
    directory does not exist on *ref* (HTTP 404 — a greenfield repo with no
    migrations) but RAISES on any other non-200 status, unexpected payload,
    or transport error so the caller defers (never waves a merge through on
    an inability to read the target branch's migrations).
    """
    entries = await _list_versions_dir_entries(
        token, owner, repo, ref, project_id=project_id, versions_dir=versions_dir
    )
    if entries is None:
        return []
    return [entry["name"] for entry in entries]


async def list_version_shas_at_ref(
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
) -> dict[str, str]:
    """Like :func:`list_versions_at_ref` but return ``{filename: git blob sha}``.

    Uses the SAME single directory-listing call — the GitHub contents API
    already returns each entry's git blob ``sha`` — so this carries no request
    cost over listing the names alone. The blob sha is git's content address:
    two refs that share a filename with an identical sha have byte-identical
    bodies, which lets the migration-identity gates skip a per-file body fetch
    for every unchanged migration (the overwhelming common case, since deployed
    migrations are immutable) instead of pulling all ~200 files on every attempt
    — the fetch storm that drains the token's rate-limit budget and wedges
    release qualification into a ``defer`` loop.

    Same fail-safe contract as :func:`list_versions_at_ref`: ``{}`` on 404,
    RAISES on any other unreadable outcome. Entries missing a string ``sha`` are
    dropped, so a caller comparing shas treats an absent entry as "unknown →
    fetch to be safe" rather than "unchanged".
    """
    entries = await _list_versions_dir_entries(
        token, owner, repo, ref, project_id=project_id
    )
    if entries is None:
        return {}
    return {
        entry["name"]: entry["sha"]
        for entry in entries
        if isinstance(entry.get("sha"), str)
    }


async def get_file_text_at_ref(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
    use_content_cache: bool = True,
) -> str | None:
    """Fetch the decoded TEXT of *path* at ``owner/repo@ref``.

    Sibling of :func:`get_file_content_at_ref` (which only answers existence +
    line count); the auto-reconcile path needs the actual source of
    ``backend/app/main.py`` to rewrite its ``EXPECTED_SCHEMA_REVISION`` literal.
    Goes through :func:`_github_request`, so it is quota-accounted and
    rate-limit-aware like every other GitHub call.

    When ``use_content_cache`` (default True) and *ref* is a full commit SHA,
    results are memoized in an in-memory LRU cache keyed by
    ``(owner, repo, ref, path)`` with a 10-minute TTL. Moving refs such as
    branch names always perform a live read so a higher-level force refresh
    cannot be defeated by stale file content.

    Returns the decoded UTF-8 text on HTTP 200, or ``None`` for any
    non-retrievable / non-decodable outcome (404, non-200, non-file payload,
    undecodable body, or transport error). Fail-safe like its read siblings:
    ``None`` never means "empty file", it means "could not read".
    """
    # Only a full commit SHA is immutable. Branch and tag names can move, so
    # caching them would make a caller's force-refresh contract ineffective.
    cacheable_ref = use_content_cache and bool(_FULL_COMMIT_SHA_RE.fullmatch(ref))

    # Check the in-memory content cache before hitting the GitHub API.
    if cacheable_ref:
        cache_key = (owner, repo, ref, path)
        try:
            cached = _file_content_cache.get(cache_key)
            if cached is not None and (time.monotonic() - cached[1]) < _FILE_CONTENT_CACHE_TTL_SECONDS:
                import app.shared_state as _ss
                _ss.github_cache_hits["read_content"] = _ss.github_cache_hits.get("read_content", 0) + 1
                return cached[0]
        except (AttributeError, KeyError, TypeError, ValueError):
            pass  # Cache miss / stale — fall through to fetch.

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
        if response.status_code != 200:
            if response.status_code != 404:
                logger.warning(
                    "[GITHUB] text fetch for %s on %s/%s@%s: HTTP %s",
                    path,
                    owner,
                    repo,
                    ref,
                    response.status_code,
                )
            return None
        payload = response.json()
        if not (isinstance(payload, dict) and payload.get("type") == "file"):
            return None
        content_b64 = payload.get("content")
        if not isinstance(content_b64, str):
            return None
        text = base64.b64decode(content_b64).decode()

        # Store in cache on success.
        if cacheable_ref and text is not None:
            try:
                cache_key = (owner, repo, ref, path)
                _file_content_cache[cache_key] = (text, time.monotonic())
                _file_content_cache.move_to_end(cache_key)  # LRU: most recently used
                # Evict oldest entries over the size cap.
                while len(_file_content_cache) > _FILE_CONTENT_CACHE_MAX_SIZE:
                    _file_content_cache.popitem(last=False)  # Least recently used
            except (AttributeError, KeyError, TypeError, ValueError):
                pass  # Cache write failure is non-critical.

        return text
    except Exception as e:  # noqa: BLE001 — never let a fetch error read as content
        logger.warning(
            "[GITHUB] Failed text fetch for %s on %s/%s@%s: %s",
            path,
            owner,
            repo,
            ref,
            e,
        )
        return None


async def get_file_bytes_at_ref(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    ref: str,
    project_id: str | None = None,
) -> bytes | None:
    """Fetch the raw BYTES of *path* at ``owner/repo@ref``.

    Binary-safe sibling of :func:`get_file_text_at_ref`. Returns the decoded
    base64 content for any file (text or binary) so that image and other binary
    asset types can be served without corruption. Goes through
    :func:`_github_request`, so it is quota-accounted and rate-limit-aware.

    Returns the raw bytes on HTTP 200, or ``None`` for any non-retrievable
    outcome (404, non-200, non-file payload, undecodable body, or transport
    error).
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
        if response.status_code != 200:
            if response.status_code != 404:
                logger.warning(
                    "[GITHUB] bytes fetch for %s on %s/%s@%s: HTTP %s",
                    path, owner, repo, ref, response.status_code,
                )
            return None
        payload = response.json()
        if not (isinstance(payload, dict) and payload.get("type") == "file"):
            return None
        content_b64 = payload.get("content")
        if not isinstance(content_b64, str):
            return None
        return base64.b64decode(content_b64)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[GITHUB] Failed bytes fetch for %s on %s/%s@%s: %s",
            path, owner, repo, ref, e,
        )
        return None


async def list_repo_tree_paths_at_ref(
    token: str,
    owner: str,
    repo: str,
    ref: str,
    *,
    project_id: str | None = None,
) -> list[dict] | None:
    """List all file paths in the repo at ``ref`` via GitHub's recursive tree API.

    Returns a list of dicts ``{"path": str, "size": int, "sha": str}`` for every
    blob in the tree (``sha`` is the git blob SHA — a stable content identity of
    the file, used by the qualifier's audited-surface content hash). Returns
    ``None`` on any error (network, 404, truncated tree — the caller decides how
    to fail-safe). A single API call retrieves the full tree so this is much more
    efficient than walking directories file-by-file.

    GitHub truncates trees with > 100,000 entries; the caller receives ``None``
    in that case so it can degrade gracefully rather than silently missing files.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    encoded_ref = quote(ref, safe="")
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/"
        f"{encoded_ref}?recursive=1"
    )
    try:
        response = await _github_request(
            "GET", url, headers, max_retries=2, project_id=project_id
        )
        if response.status_code != 200:
            logger.warning(
                "[GITHUB] tree listing for %s/%s@%s: HTTP %s",
                owner,
                repo,
                ref,
                response.status_code,
            )
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        if payload.get("truncated"):
            logger.warning(
                "[GITHUB] tree listing for %s/%s@%s was truncated (>100k entries)",
                owner,
                repo,
                ref,
            )
            return None
        tree = payload.get("tree")
        if not isinstance(tree, list):
            return None
        return [
            {
                "path": entry["path"],
                "size": entry.get("size", 0),
                "sha": entry.get("sha"),
            }
            for entry in tree
            if isinstance(entry, dict) and entry.get("type") == "blob"
        ]
    except Exception as e:  # noqa: BLE001 — never crash the caller on a tree-list error
        logger.warning(
            "[GITHUB] tree listing for %s/%s@%s raised: %s",
            owner,
            repo,
            ref,
            e,
        )
        return None


async def commit_file_changes(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    *,
    upserts: dict[str, str],
    deletes: list[str] | None = None,
    message: str,
    project_id: str | None = None,
    expected_head_sha: str | None = None,
) -> str:
    """Commit *upserts* (path -> text) and *deletes* to *branch* atomically.

    Uses the low-level Git Data API so an arbitrary set of file changes lands in
    ONE commit (vs. the contents API's one-commit-per-file): read the branch's
    tip + base tree, create a blob per upsert, build a new tree on top of the
    base tree (deletions are entries with ``sha: null``), create a commit
    parented on the tip, then fast-forward ``heads/{branch}`` to it.

    ``expected_head_sha`` turns the write into a compare-and-swap operation: if
    the branch moved after the caller read its inputs, no blobs or commits are
    created and :class:`StaleBranchHeadError` is raised. If the requested tree
    is already the current tree, the current tip SHA is returned without
    creating an empty commit. Otherwise returns the new commit SHA. RAISES
    ``RuntimeError`` on any unexpected GitHub status.
    """
    deletes = deletes or []
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{owner}/{repo}"

    def _json(resp: httpx.Response, expected: int, what: str) -> dict:
        if resp.status_code != expected:
            raise RuntimeError(
                f"GitHub Git Data API {what} on {owner}/{repo}@{branch} "
                f"returned HTTP {resp.status_code} (expected {expected})"
            )
        return resp.json()

    # 1. Current branch tip commit.
    ref_resp = await _github_request(
        "GET",
        f"{base}/git/ref/heads/{quote(branch, safe='')}",
        headers,
        project_id=project_id,
    )
    tip_sha = _json(ref_resp, 200, "get-ref")["object"]["sha"]
    if expected_head_sha is not None and tip_sha != expected_head_sha:
        raise StaleBranchHeadError(
            f"stale branch head for {owner}/{repo}@{branch}: "
            f"expected {expected_head_sha}, found {tip_sha}"
        )

    # 2. Base tree of that commit.
    commit_resp = await _github_request(
        "GET", f"{base}/git/commits/{tip_sha}", headers, project_id=project_id
    )
    base_tree_sha = _json(commit_resp, 200, "get-commit")["tree"]["sha"]

    # 3. A blob per upsert.
    tree_entries: list[dict] = []
    for path, content in upserts.items():
        blob_resp = await _github_request(
            "POST",
            f"{base}/git/blobs",
            headers,
            json={"content": content, "encoding": "utf-8"},
            project_id=project_id,
        )
        blob_sha = _json(blob_resp, 201, "create-blob")["sha"]
        tree_entries.append(
            {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
        )
    # Deletions: a tree entry with sha null removes the path.
    tree_entries.extend(
        {"path": path, "mode": "100644", "type": "blob", "sha": None}
        for path in deletes
    )

    # 4. New tree on top of the existing base tree.
    tree_resp = await _github_request(
        "POST",
        f"{base}/git/trees",
        headers,
        json={"base_tree": base_tree_sha, "tree": tree_entries},
        project_id=project_id,
    )
    new_tree_sha = _json(tree_resp, 201, "create-tree")["sha"]

    # GitHub accepts a commit whose tree is identical to its parent's tree.
    # Under concurrent auto-remediation that turns a stale cached write into an
    # unbounded stream of empty commits, so stop before create-commit/update-ref.
    if new_tree_sha == base_tree_sha:
        logger.info(
            "[GITHUB] commit_file_changes no-op on %s/%s@%s tip=%s; "
            "requested tree already present",
            owner,
            repo,
            branch,
            tip_sha[:12],
        )
        return tip_sha

    # 5. Commit parented on the current tip.
    commit_create_resp = await _github_request(
        "POST",
        f"{base}/git/commits",
        headers,
        json={"message": message, "tree": new_tree_sha, "parents": [tip_sha]},
        project_id=project_id,
    )
    new_commit_sha = _json(commit_create_resp, 201, "create-commit")["sha"]

    # 6. Fast-forward the branch ref to the new commit.
    update_resp = await _github_request(
        "PATCH",
        f"{base}/git/refs/heads/{quote(branch, safe='')}",
        headers,
        json={"sha": new_commit_sha, "force": False},
        project_id=project_id,
    )
    _json(update_resp, 200, "update-ref")
    return new_commit_sha


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
    get_repo_installation_read_token,
    exchange_oauth_code,
    get_app_info,
    get_user_installations,
    get_github_user,
    generate_installation_access_token,
    validate_fine_grained_pat,
    invalidate_repo_installation_token_cache,
    invalidate_repo_installation_read_token_cache,
    _INSTALLATION_ID_TTL_SECONDS,
    _installation_id_cache,
)
from app.github_checks import (  # noqa: F401
    update_commit_status,
    create_check_run,
    update_check_run,
    _unique,
    get_check_runs,
    get_workflow_run_for_sha,
    get_latest_inflight_workflow_run_for_branch,
    AGENT_BAKE_WORKFLOW_FILE,
    CI_WORKFLOW_FILE,
    dispatch_workflow_run,
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
    merge_branch,
    get_branch_sha,
    get_repo_default_branch,
    repo_has_commits,
    ensure_repo_initialized,
    delete_branch,
    list_branches,
    get_commit_date,
    list_pull_requests_for_commit,
    is_ancestor_of,
    compare_ref_status,
    list_recent_commit_messages,
    check_squash_referenced,
    get_files_changed_between_refs,
    agent_image_rebake_expected,
    AGENT_IMAGE_BAKE_PATHS,
)
from app.github_pulls import (  # noqa: F401
    pr_is_not_behind,
    pr_is_not_behind_for,
    create_pull_request,
    get_pull_request,
    get_branch_head_sha,
    get_pull_request_review_decision,
    get_pull_request_reviews_checked,
    REVIEWS_COMPLETE,
    REVIEWS_TRUNCATED,
    REVIEWS_FAILED,
    get_open_pull_requests,
    get_open_pull_request_for_head_branch,
    get_pull_request_diff,
    get_pull_request_commits,
    merge_pull_request,
    get_pull_request_merge_state,
    update_branch,
    update_branch_with_sha_retry,
    close_pull_request,
    reopen_pull_request,
    get_pr_files,
    pr_effective_diff_is_empty,
    _UPDATE_BRANCH_NO_NEW_COMMITS_PHRASE,
    _is_no_new_commits_message,
    _tag_not_behind_if_no_new_commits,
    get_pull_requests_for_head_branch,
)
from app.github_merge_summary import (  # noqa: F401
    build_merge_summary_body,
    post_merge_summary_comment,
    build_blueprint_pr_merge_summary_comment,
    post_pipeline_merge_summary_comment,
    BLUEPRINT_PR_MERGE_SUMMARY_MARKER,
)
