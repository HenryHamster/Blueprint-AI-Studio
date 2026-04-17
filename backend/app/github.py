import asyncio
import random
from typing import Any, Dict

import httpx
from fastapi import HTTPException, status
import logging
import os
import time
import jwt

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


import re as _re


def _normalize_endpoint_template(method: str, url: str) -> str:
    """Collapse a concrete GitHub URL into an endpoint template for counting.

    ``GET https://api.github.com/repos/foo/bar/pulls/1852`` becomes
    ``GET /repos/{owner}/{repo}/pulls/{n}`` so per-call counters don't
    explode on PR numbers, SHAs, or branch refs.  The mapping is a
    handful of regexes — cheap, doesn't require parsing the URL.
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
    import app.shared_state as _ss  # noqa: PLC0415
    template = _normalize_endpoint_template(method, url)
    _ss.github_call_counts[template] = _ss.github_call_counts.get(template, 0) + 1


async def probe_rate_limit() -> bool:
    """Ping GitHub's /rate_limit endpoint to keep github_quota fresh.

    /rate_limit is a free call — doesn't count against the 5000/hr REST
    budget (per GitHub docs).  We use it as a heartbeat so the dashboard
    quota pill shows real numbers even when the pipeline is idle and no
    task-driven GitHub calls have happened recently.

    Uses a cached installation token (not App JWT) — GitHub reports
    SEPARATE rate-limit buckets per token type, and the pill is only
    meaningful for the installation-token bucket (which is what every
    task-driven call uses).  Probing with App JWT populates the pill
    with the App's narrower ~60/hr bucket, making it look like quota
    is exhausted when in fact the 5000/hr install bucket is fine.

    Returns True on success, False if no cached install token is
    available (in which case the pill waits for organic task traffic).
    """
    # Pull any currently-cached installation token — doesn't spend quota
    # on a refresh.  If nothing's cached, skip the probe; real activity
    # will populate the pill soon enough.
    from app.routers.agent import _github_token_cache  # noqa: PLC0415
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
            "GET", "https://api.github.com/rate_limit", headers,
            max_retries=1,
        )
        return resp is not None
    except Exception as e:
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
    import app.shared_state as _ss  # noqa: PLC0415
    auth = (headers or {}).get("Authorization", "")
    if "/app/" in url or auth.startswith("Bearer eyJ"):
        return _ss.github_jwt_quota
    return _ss.github_quota


def _update_quota_state(
    remaining: int, limit: int | None, reset_ts: int | None,
    *, bucket_name: str = "install",
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
    import app.shared_state as _ss  # noqa: PLC0415
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
        import app.shared_state as _ss  # noqa: PLC0415
        label = "JWT" if bucket is _ss.github_jwt_quota else "install-token"
        _ss.github_cache_hits["bucket_short_circuit"] = _ss.github_cache_hits.get("bucket_short_circuit", 0) + 1
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
                and (response.headers.get("X-RateLimit-Remaining") == "0"
                     or response.headers.get("Retry-After") is not None)
            )
            if is_rate_limited and attempt < max_retries - 1:
                retry_after = response.headers.get("Retry-After")
                reset_ts = response.headers.get("X-RateLimit-Reset")
                if retry_after:
                    delay = min(int(retry_after), 120)
                elif reset_ts:
                    delay = min(max(0, int(reset_ts) - int(time.time())) + 1, 120)
                else:
                    delay = min(2 ** attempt * 10, 120)
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
                            logger.warning(f"[GITHUB] Rate limit critical: {remaining} calls left, waiting {wait}s until reset")
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
                    try:
                        _update_quota_state(
                            remaining=int(remaining),
                            limit=int(response.headers.get("X-RateLimit-Limit", 0)) or None,
                            reset_ts=int(response.headers.get("X-RateLimit-Reset", 0)) or None,
                            bucket_name=_bucket_name,
                        )
                    except (ValueError, TypeError):
                        pass  # Malformed headers, skip
            if response.status_code == 401:
                logger.warning(
                    f"[GITHUB] {method} {url} returned 401 Unauthorized — "
                    f"token may be expired or revoked"
                )
                if project_id:
                    from app.routers.agent import invalidate_github_token_cache  # noqa: PLC0415
                    invalidate_github_token_cache(project_id)
                    logger.info(f"[GITHUB] Invalidated token cache for project {project_id} after 401")
            if response.status_code < 500 or attempt == max_retries - 1:
                if attempt > 0 and response.status_code >= 500:
                    logger.error(
                        f"[GITHUB] {method} {url} failed with {response.status_code} "
                        f"after {max_retries} attempts"
                    )
                return response
            delay = 2 ** attempt * (0.5 + random.random() * 0.5)  # jittered: 0.5x–1x base
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
            if isinstance(exc, (httpx.ConnectError, httpx.ReadError,
                                httpx.WriteError, httpx.PoolTimeout)):
                await close_github_client()
                logger.warning(f"[GITHUB] Reset HTTP client after transport error: {type(exc).__name__}")
            delay = 2 ** attempt * (0.5 + random.random() * 0.5)  # jittered: 0.5x–1x base
            logger.warning(
                f"[GITHUB] {method} {url} network error: {exc}, "
                f"retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(delay)
    assert response is not None  # loop always runs at least once (max_retries >= 1)
    return response  # unreachable but satisfies type checker


def generate_github_app_jwt() -> str:
    """
    Generates a JWT to authenticate as the GitHub App.
    """
    if not GITHUB_APP_ID or not GITHUB_PRIVATE_KEY:
        raise ValueError("GITHUB_APP_ID and GITHUB_PRIVATE_KEY must be set in environment variables.")

    now = int(time.time())
    payload = {
        "iat": now - 60, # Issued at time, 60 seconds in the past to allow for clock drift
        "exp": now + (10 * 60), # JWT expiration time (10 minute maximum)
        "iss": GITHUB_APP_ID # GitHub App's identifier
    }

    # Ensure the private key is properly formatted
    private_key = GITHUB_PRIVATE_KEY.replace('\\n', '\n')

    encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")
    return encoded_jwt

# Cache of (owner, repo) → installation_id.  Installations don't change
# often (only when a repo is added/removed from the App), so a long TTL
# is safe.  Cuts the "discover + generate" path from 2 API calls to 1
# for every token refresh after the first one on a given Cloud Run
# instance.  Invalidated by a 404 from installation lookup OR a 401 on
# the generated token (handled by the general invalidate-cache flow).
_INSTALLATION_ID_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_installation_id_cache: dict[str, tuple[str, float]] = {}


async def get_repo_installation_token(owner: str, repo: str) -> str | None:
    """
    Discovers the GitHub App installation for a specific repo and returns an access token.
    Used as a fallback when no UserIntegration exists (e.g., admin-created tasks).
    Returns None if the App is not installed on the repo.

    Two API calls on cold cache (GET installation, POST access_tokens).
    One API call on warm cache (installation_id cached, POST access_tokens only).
    """
    if not GITHUB_APP_ID or not GITHUB_PRIVATE_KEY:
        return None

    cache_key = f"{owner}/{repo}"
    cached = _installation_id_cache.get(cache_key)
    installation_id: str | None = None
    if cached and time.time() - cached[1] < _INSTALLATION_ID_TTL_SECONDS:
        installation_id = cached[0]

    if installation_id is None:
        app_jwt = generate_github_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        resp = await _github_request(
            "GET", f"https://api.github.com/repos/{owner}/{repo}/installation", headers
        )
        if resp.status_code != 200:
            logger.warning(f"No GitHub App installation found for {owner}/{repo}: {resp.status_code}")
            _installation_id_cache.pop(cache_key, None)
            return None
        installation_id = str(resp.json()["id"])
        _installation_id_cache[cache_key] = (installation_id, time.time())

    return await generate_installation_access_token(installation_id, repositories=[repo])


async def exchange_oauth_code(code: str, redirect_uri: str | None = None) -> dict:
    """Exchange a GitHub OAuth code for a user access token."""
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise ValueError("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET must be set")

    payload = {"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET, "code": code}
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri

    resp = await _github_request(
        "POST", "https://github.com/login/oauth/access_token",
        {"Accept": "application/json"},
        json=payload,
    )
    if resp.status_code != 200:
        logger.error(f"[GITHUB OAUTH] Token exchange failed ({resp.status_code}): {resp.text}")
        raise HTTPException(status_code=502, detail="GitHub OAuth exchange failed")
    data = resp.json()
    if "error" in data:
        logger.error(f"[GITHUB OAUTH] Error in token data: {data}")
        raise HTTPException(status_code=400, detail=f"GitHub OAuth error: {data.get('error_description', data['error'])}")
    return data


async def get_app_info() -> dict:
    """Fetch basic information about this GitHub App."""
    if not GITHUB_APP_ID or not GITHUB_PRIVATE_KEY:
        raise ValueError("GITHUB_APP_ID and GITHUB_PRIVATE_KEY must be set")

    app_jwt = generate_github_app_jwt()
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    resp = await _github_request("GET", "https://api.github.com/app", headers)
    if resp.status_code != 200:
        logger.error(f"Failed to fetch app info: {resp.text}")
        raise HTTPException(status_code=resp.status_code, detail=f"GitHub API error: {resp.text}")
    return resp.json()


async def get_user_installations(user_token: str) -> list[dict]:
    """Fetch the GitHub App installations accessible to the authenticated user."""
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    installations = []
    url: str | None = "https://api.github.com/user/installations?per_page=100"
    while url:
        resp = await _github_request("GET", url, headers)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch user installations: {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=f"GitHub API error: {resp.text}")
        data = resp.json()
        installations.extend(data.get("installations", []))
        # Follow pagination
        url = resp.links.get("next", {}).get("url")
    return installations


async def get_github_user(user_token: str) -> dict:
    """Get the authenticated GitHub user's profile."""
    resp = await _github_request(
        "GET", "https://api.github.com/user",
        {"Authorization": f"Bearer {user_token}", "Accept": "application/vnd.github.v3+json"},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch GitHub user profile")
    return resp.json()


async def generate_installation_access_token(installation_id: str, repositories: list[str] | None = None) -> str:
    """
    Exchanges the GitHub App JWT for a short-lived Installation Access Token.
    """
    app_jwt = generate_github_app_jwt()

    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload = {}
    if repositories:
        payload["repositories"] = repositories

    try:
        response = await _github_request(
            "POST",
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers, json=payload
        )
    except httpx.HTTPError as e:
        logger.exception("Network error generating installation token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to reach GitHub API: {e}"
        )

    if response.status_code != 201:
        logger.error(f"Failed to generate installation access token: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to authenticate with GitHub App ({response.status_code}): {response.text[:200]}"
        )

    return response.json().get("token")

async def validate_fine_grained_pat(token: str, target_repo: str | None = None) -> dict:
    """
    Validates that a GitHub Personal Access Token is:
    1. A Fine-Grained PAT (starts with github_pat_)
    2. Valid and active
    3. (Optional) Has access to the specified target repository.
    """
    if not token.startswith("github_pat_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token format. Please provide a Fine-Grained Personal Access Token (starts with 'github_pat_'). Classic tokens ('ghp_') are not allowed for security reasons."
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # 1. Check if the token is valid by hitting the authenticated user endpoint
    user_response = await _github_request("GET", "https://api.github.com/user", headers)

    if user_response.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The provided GitHub token is invalid or expired."
        )
    elif user_response.status_code != 200:
        logger.error(f"GitHub API error: {user_response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate token with GitHub."
        )

    # 2. If a target repo is provided, verify the token can access it
    if target_repo:
        repo_response = await _github_request("GET", f"https://api.github.com/repos/{target_repo}", headers)
        if repo_response.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"The token does not have access to the repository '{target_repo}'. Ensure the Fine-Grained PAT is scoped to this repository."
            )
        elif repo_response.status_code != 200:
            logger.error(f"GitHub API error on repo check: {repo_response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to verify repository access with GitHub."
            )

        # Optionally check if the token has push access
        repo_data = repo_response.json()
        permissions = repo_data.get("permissions", {})
        if not permissions.get("push", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"The token has read access to '{target_repo}', but lacks 'push' (write) permissions. Please update the token scopes."
            )

    return {
        "status": "valid",
        "message": "Fine-Grained PAT is valid and properly scoped.",
        "user": user_response.json().get("login")
    }

async def update_commit_status(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    state: str,
    description: str = "",
    context: str = "blueprint-agent",
    target_url: str = ""
) -> dict:
    """
    Reports agent task progress (Queued, In Progress, Success, Failed) directly to the GitHub Commit Status API.
    state can be one of: 'error', 'failure', 'pending', or 'success'.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload = {
        "state": state,
        "description": description,
        "context": context
    }
    if target_url:
        payload["target_url"] = target_url

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/statuses/{sha}",
        headers, json=payload
    )

    if response.status_code != 201:
        logger.error(f"Failed to update commit status: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update commit status on GitHub."
        )

    return response.json()

async def create_check_run(
    token: str,
    owner: str,
    repo: str,
    head_sha: str,
    name: str = "Blueprint Code Review",
    details_url: str = "",
) -> dict | None:
    """Create a Check Run for a commit in 'in_progress' state.

    Returns the check run dict (including its id) on success, or None on error.
    Uses the Checks API (POST /repos/{owner}/{repo}/check-runs).
    Requires a GitHub App token with checks:write permission.
    """
    import datetime as _dt
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: dict = {
        "name": name,
        "head_sha": head_sha,
        "status": "in_progress",
        "started_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if details_url:
        payload["details_url"] = details_url

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/check-runs",
        headers, json=payload
    )

    if response.status_code == 201:
        return response.json()
    logger.warning(
        f"[GITHUB] create_check_run failed for {owner}/{repo} sha={head_sha[:8]}: "
        f"{response.status_code} {response.text[:200]}"
    )
    return None


async def update_check_run(
    token: str,
    owner: str,
    repo: str,
    check_run_id: int,
    conclusion: str,
    title: str,
    summary: str,
    annotations: list[dict] | None = None,
) -> dict | None:
    """Update an existing Check Run to 'completed' with a conclusion.

    conclusion must be one of: 'success', 'failure', 'neutral', 'cancelled',
    'skipped', 'timed_out', 'action_required'.

    annotations is an optional list of dicts with keys:
        path, start_line, end_line, annotation_level ('notice'|'warning'|'failure'),
        message.  GitHub caps annotations at 50 per request.
    """
    import datetime as _dt
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    output: dict = {
        "title": title,
        "summary": summary[:65535],
    }
    if annotations:
        output["annotations"] = annotations[:50]

    payload: dict = {
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output": output,
    }

    response = await _github_request(
        "PATCH", f"https://api.github.com/repos/{owner}/{repo}/check-runs/{check_run_id}",
        headers, json=payload
    )

    if response.status_code == 200:
        return response.json()
    logger.warning(
        f"[GITHUB] update_check_run failed for {owner}/{repo} run_id={check_run_id}: "
        f"{response.status_code} {response.text[:200]}"
    )
    return None


async def create_pr_comment(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    body: str
) -> dict:
    """
    Posts a general PR summary or error log back to a GitHub PR.
    Note: GitHub treats PRs as issues for general comments.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload = {
        "body": body
    }

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        headers, json=payload
    )

    if response.status_code != 201:
        logger.error(f"Failed to create PR comment: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create PR comment on GitHub."
        )

    return response.json()

async def create_pr_review_comment(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    path: str,
    line: int,
    body: str
) -> dict:
    """
    Posts an inline code review comment on a specific line of a file in a PR.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload = {
        "body": body,
        "commit_id": commit_id,
        "path": path,
        "line": line,
        "side": "RIGHT"
    }

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments",
        headers, json=payload
    )

    if response.status_code != 201:
        logger.error(f"Failed to create PR review comment: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create PR review comment on GitHub."
        )

    return response.json()


async def submit_pr_review(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    event: str,
    body: str,
    comments: list[dict]
) -> dict | None:
    """
    Submits a PR review via the GitHub Pull Request Reviews API.

    Fetches the HEAD commit SHA first, then POSTs the review.
    If inline comments cause a 422, retries without them so a review is always posted.
    Returns None on 404.
    """
    pr_data = await get_pull_request(token, owner, repo, pr_number)
    sha = pr_data["head"]["sha"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload: dict = {
        "commit_id": sha,
        "event": event,
        "body": body,
        "comments": comments,
    }

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        headers, json=payload
    )

    if response.status_code == 422:
        logger.warning(
            f"[GITHUB] submit_pr_review 422 for PR #{pr_number} — retrying without inline comments: {response.text[:300]}"
        )
        payload["comments"] = []
        response = await _github_request(
            "POST", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers, json=payload
        )

    if response.status_code == 404:
        logger.warning(f"[GITHUB] submit_pr_review 404 for PR #{pr_number} in {owner}/{repo}")
        return None

    if response.status_code not in (200, 201):
        logger.error(f"[GITHUB] Failed to submit PR review #{pr_number}: {response.status_code} {response.text[:300]}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit PR review on GitHub: {response.text[:200]}"
        )

    return response.json()

async def create_issue(
    token: str,
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    assignees: list | None = None
) -> dict:
    """
    Creates a new issue in the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload: Dict[str, Any] = {
        "title": title,
        "body": body
    }
    if assignees:
        payload["assignees"] = assignees

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers, json=payload
    )

    if response.status_code != 201:
        logger.error(f"Failed to create issue: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create issue on GitHub."
        )

    return response.json()

async def get_issues(
    token: str,
    owner: str,
    repo: str,
    state: str = "open"
) -> list:
    """
    Retrieves a list of issues for the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/issues?state={state}",
        headers
    )

    if response.status_code != 200:
        logger.error(f"Failed to get issues: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve issues from GitHub."
        )

    return response.json()

async def get_single_issue(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
) -> dict:
    """Fetch a single issue by number."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        headers,
    )

    if response.status_code != 200:
        logger.error(f"Failed to get issue #{issue_number}: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve issue #{issue_number} from GitHub.",
        )

    return response.json()


async def get_issue_comments(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    per_page: int = 30,
) -> list:
    """Fetch comments on an issue. Returns [] on any failure (graceful degradation)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments?per_page={per_page}",
            headers,
        )
        if response.status_code != 200:
            logger.warning(f"Failed to get comments for issue #{issue_number}: {response.status_code}")
            return []
        return response.json()
    except Exception as exc:
        logger.warning(f"Error fetching comments for issue #{issue_number}: {exc}")
        return []


async def create_issue_comment(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> dict:
    """Post a comment on an issue (GitHub treats PRs as issues for comments)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
        headers,
        json={"body": body},
    )

    if response.status_code != 201:
        logger.error(f"Failed to create comment on issue #{issue_number}: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create comment on issue #{issue_number}.",
        )

    return response.json()


async def create_branch(
    token: str,
    owner: str,
    repo: str,
    branch_name: str,
    sha: str
) -> dict:
    """
    Creates a new branch (git reference) in the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload = {
        "ref": f"refs/heads/{branch_name}",
        "sha": sha
    }

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        headers, json=payload
    )

    if response.status_code == 422:
        # Branch may already exist (e.g., retry after 502 that actually succeeded)
        try:
            existing_sha = await get_branch_sha(token, owner, repo, branch_name)
            logger.info(f"Branch '{branch_name}' already exists (sha: {existing_sha[:8]}), reusing")
            return {"ref": f"refs/heads/{branch_name}", "object": {"sha": existing_sha}}
        except Exception:
            logger.exception(f"Branch '{branch_name}' 422 but get_branch_sha also failed; falling through")

    if response.status_code != 201:
        logger.error(f"Failed to create branch: {response.status_code} {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create branch '{branch_name}' on GitHub (GitHub {response.status_code})."
        )

    return response.json()


async def get_branch_sha(
    token: str, owner: str, repo: str, branch: str
) -> str:
    """Gets the latest commit SHA of a branch."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
        headers
    )
    if response.status_code == 404:
        logger.warning(f"Branch '{branch}' not found (404)")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch '{branch}' not found (GitHub 404)."
        )
    if response.status_code != 200:
        logger.error(f"Failed to get SHA for branch {branch}: {response.status_code} {response.text}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error getting branch '{branch}' SHA (HTTP {response.status_code})."
        )
    return response.json()["object"]["sha"]


async def get_repo_default_branch(token: str, owner: str, repo: str) -> str:
    """Get the repository's default branch name (usually 'main' or 'master')."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}", headers
    )
    if response.status_code != 200:
        return "main"  # Safe fallback
    return response.json().get("default_branch", "main")


async def delete_branch(
    token: str, owner: str, repo: str, branch: str
) -> bool:
    """Deletes a branch (git reference). Returns True if deleted."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "DELETE", f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
        headers
    )
    if response.status_code == 204:
        return True
    logger.warning(f"Failed to delete branch {branch}: {response.status_code}")
    return False


async def list_branches(
    token: str, owner: str, repo: str,
    prefixes: list | None = None,
    max_branches: int = 500,
) -> list:
    """List repository branches, optionally filtered by name prefixes.

    Returns a list of dicts with 'name' and 'sha' keys.
    Returns empty list on auth error or API failure.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    branches = []
    url = f"https://api.github.com/repos/{owner}/{repo}/branches?per_page=100"

    while url and len(branches) < max_branches:
        response = await _github_request("GET", url, headers)
        if response.status_code == 401:
            raise HTTPException(status_code=401, detail="GitHub authentication failed (401 Unauthorized)")
        if response.status_code != 200:
            logger.error(f"[GITHUB] Failed to list branches for {owner}/{repo}: {response.status_code}")
            return []
        data = response.json()
        for b in data:
            name = b["name"]
            if prefixes is None or any(name.startswith(p) for p in prefixes):
                branches.append({"name": name, "sha": b["commit"]["sha"]})
        url = response.links.get("next", {}).get("url")

    return branches


async def get_commit_date(
    token: str, owner: str, repo: str, sha: str
) -> "datetime | None":
    """Get the committer date for a commit SHA. Returns None on error."""
    from datetime import datetime as _dt
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}", headers
    )
    if response.status_code != 200:
        return None
    date_str = response.json().get("commit", {}).get("committer", {}).get("date")
    if not date_str:
        return None
    try:
        return _dt.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def create_pull_request(
    token: str,
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    project_id: str | None = None,
) -> dict:
    """
    Creates a new pull request in the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload = {
        "title": title[:256],
        "head": head,
        "base": base,
        "body": body[:65536]
    }

    response = await _github_request(
        "POST", f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers, json=payload, project_id=project_id,
    )

    if response.status_code == 201:
        return response.json()

    # 422 often means a PR already exists or there's nothing to merge
    if response.status_code == 422:
        data = response.json()
        errors = data.get("errors", [])
        for err in errors:
            msg = str(err.get("message", "")).lower()
            if "pull request already exists" in msg:
                # Return the existing PR instead of failing
                logger.info(f"PR already exists for {head} -> {base}, fetching existing PR")
                list_resp = await _github_request(
                    "GET",
                    f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{head}&base={base}&state=open",
                    headers, project_id=project_id,
                )
                if list_resp.status_code == 200 and list_resp.json():
                    return list_resp.json()[0]
            if "no commits between" in msg:
                # Empty diff — branches are identical. Return a synthetic "merged" result
                # so the caller can mark the task as completed instead of failed.
                logger.info(f"[PR] No commits between {head} and {base} — empty diff")
                return {"empty_diff": True, "merged": True, "html_url": "", "number": 0}

    if response.status_code == 401:
        logger.error(f"Failed to create pull request (401 Unauthorized): {response.text}")
        raise HTTPException(
            status_code=401,
            detail=f"GitHub authentication failed (401 Unauthorized) — token may be expired or revoked"
        )
    logger.error(f"Failed to create pull request ({response.status_code}): {response.text}")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Failed to create pull request: {response.text[:500]}"
    )


async def get_pull_request(
    token: str, owner: str, repo: str, pr_number: int,
    project_id: str | None = None,
) -> dict:
    """Fetches PR details."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers, project_id=project_id,
    )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail=f"GitHub authentication failed (401 Unauthorized) — token may be expired or revoked")
    if response.status_code in (404, 410):
        raise HTTPException(status_code=response.status_code, detail=f"PR #{pr_number} not found or repo gone")
    if response.status_code != 200:
        logger.error(f"Failed to get PR #{pr_number}: {response.text}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get PR #{pr_number}")
    return response.json()


async def get_open_pull_requests(
    token: str, owner: str, repo: str, base: str | None = None
) -> list[dict]:
    """Fetches open PRs, optionally filtered to a specific base branch."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    params: dict[str, str | int] = {"state": "open", "per_page": 100}
    if base is not None:
        params["base"] = base
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers, params=params
    )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub authentication failed (401 Unauthorized)")
    if response.status_code != 200:
        logger.error(f"Failed to list PRs (base={base}): {response.text}")
        return []
    return response.json()


async def get_pull_request_diff(
    token: str, owner: str, repo: str, pr_number: int
) -> str:
    """Fetches the raw diff for a PR."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers, timeout=30.0
    )
    if response.status_code != 200:
        logger.error(f"Failed to get PR diff #{pr_number}: {response.text}")
        return ""
    return response.text


async def get_pull_request_commits(
    token: str, owner: str, repo: str, pr_number: int
) -> list:
    """Fetches commit data for a PR (messages, SHAs)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits?per_page=100",
        headers
    )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub authentication failed (401 Unauthorized)")
    if response.status_code != 200:
        logger.error(f"Failed to get PR commits #{pr_number}: {response.text}")
        return []
    return response.json()


async def merge_pull_request(
    token: str, owner: str, repo: str, pr_number: int, merge_method: str = "squash",
    project_id: str | None = None,
) -> dict:
    """Merges a PR. Returns dict with 'merged' bool and 'message'. Does not raise on conflict."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "PUT", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
        headers, json={"merge_method": merge_method}, project_id=project_id,
    )
    data = response.json()
    if response.status_code == 200:
        return {"merged": True, "message": data.get("message", "Merged")}
    if response.status_code in (405, 409):
        # 405 = not mergeable (checks/reviews), 409 = merge conflict
        msg = data.get("message", "Merge failed")
        logger.warning(f"PR #{pr_number} merge blocked ({response.status_code}): {msg}")
        return {"merged": False, "conflict": response.status_code == 409, "message": msg}
    body_snippet = (response.text or "")[:500]
    logger.error(
        f"Failed to merge PR #{pr_number} (status={response.status_code}): {body_snippet}"
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Failed to merge PR #{pr_number} (upstream status {response.status_code})",
    )


async def enable_auto_merge(
    token: str, owner: str, repo: str, pr_number: int, merge_method: str = "SQUASH",
    project_id: str | None = None,
) -> dict:
    """Enable GitHub auto-merge on a PR via the GraphQL enablePullRequestAutoMerge mutation.

    Auto-merge queues the PR to merge automatically once branch-protection gates
    pass (CI, required reviews, etc.). It is a defensive layer for the imperative
    merge_pull_request — when merge_pull_request fails with 405 because a check is
    still pending, calling this afterwards lets GitHub complete the merge when
    the gate clears, so the task doesn't have to keep retrying.

    Works when mergeable_state is CLEAN, UNSTABLE, or BLOCKED. Fails on DIRTY
    (real merge conflict) or BEHIND — the caller should check mergeable_state
    before calling rather than relying on this function to surface that.

    Does not raise on GraphQL-level or PR-level errors; returns
    {"enabled": bool, "message": str} mirroring merge_pull_request's contract.
    """
    merge_method = (merge_method or "SQUASH").upper()
    if merge_method not in ("SQUASH", "MERGE", "REBASE"):
        return {"enabled": False, "message": f"Invalid merge_method: {merge_method}"}

    # Fetch node_id via the REST PR endpoint — GraphQL needs the global ID.
    try:
        pr_data = await get_pull_request(token, owner, repo, pr_number, project_id=project_id)
    except HTTPException as exc:
        return {"enabled": False, "message": f"Failed to fetch PR #{pr_number}: {exc.detail}"}
    pr_node_id = pr_data.get("node_id")
    if not pr_node_id:
        return {"enabled": False, "message": f"PR #{pr_number} has no node_id in REST response"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    mutation = (
        "mutation($prId: ID!, $method: PullRequestMergeMethod!) {"
        "  enablePullRequestAutoMerge(input: {pullRequestId: $prId, mergeMethod: $method}) {"
        "    pullRequest { autoMergeRequest { enabledAt } }"
        "  }"
        "}"
    )
    body = {"query": mutation, "variables": {"prId": pr_node_id, "method": merge_method}}

    response = await _github_request(
        "POST", "https://api.github.com/graphql",
        headers, json=body, project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(f"[AUTO-MERGE] PR #{pr_number} GraphQL {response.status_code}: {response.text[:200]}")
        return {"enabled": False, "message": f"GraphQL HTTP {response.status_code}"}
    data = response.json() or {}
    # GraphQL returns 200 even on application-level errors — check the errors array.
    if data.get("errors"):
        err_msgs = "; ".join(str(e.get("message", "")) for e in data["errors"])
        logger.info(f"[AUTO-MERGE] PR #{pr_number} rejected by GitHub: {err_msgs}")
        return {"enabled": False, "message": err_msgs or "GraphQL error"}
    enabled_at = (
        data.get("data", {}).get("enablePullRequestAutoMerge", {})
        .get("pullRequest", {}).get("autoMergeRequest", {}) or {}
    ).get("enabledAt")
    if enabled_at:
        logger.info(f"[AUTO-MERGE] PR #{pr_number} queued for auto-merge ({merge_method}) at {enabled_at}")
        return {"enabled": True, "message": f"Auto-merge enabled at {enabled_at}"}
    return {"enabled": False, "message": "GraphQL returned no enabledAt — auto-merge not confirmed"}


async def update_branch(
    token: str, owner: str, repo: str, pr_number: int,
    expected_head_sha: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Update a PR branch with the latest base-branch commits via
    PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch.

    Use this when a PR's mergeable_state is 'behind' — GitHub will create a
    merge commit from base into the PR branch, which triggers a fresh CI run
    and clears the 'behind' state. Does not raise on expected failures (422
    cannot-update, 403/404 auth/no-PR); returns {"updated": bool, "message": str}
    mirroring merge_pull_request / enable_auto_merge.

    Raises on unexpected 5xx so the caller can treat it as transient.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body: dict[str, str] = {}
    if expected_head_sha:
        body["expected_head_sha"] = expected_head_sha
    response = await _github_request(
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/update-branch",
        headers, json=body, project_id=project_id,
    )
    if response.status_code == 202:
        try:
            data = response.json() or {}
        except Exception:
            data = {}
        msg = data.get("message", "Update queued")
        logger.info(f"[UPDATE-BRANCH] PR #{pr_number} update queued: {msg}")
        return {"updated": True, "message": msg}
    if response.status_code in (403, 404, 422):
        try:
            data = response.json() or {}
        except Exception:
            data = {}
        msg = data.get("message", f"HTTP {response.status_code}")
        logger.info(f"[UPDATE-BRANCH] PR #{pr_number} cannot update ({response.status_code}): {msg}")
        return {"updated": False, "message": msg}
    body_snippet = (response.text or "")[:500]
    logger.error(
        f"Failed to update PR #{pr_number} branch (status={response.status_code}): {body_snippet}"
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Failed to update PR #{pr_number} branch (upstream status {response.status_code})",
    )


async def update_branch_with_sha_retry(
    token: str, owner: str, repo: str, pr_number: int,
    expected_head_sha: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Call update_branch; on any non-success, refetch the PR and retry once with a fresh SHA.

    GitHub returns 422 for update-branch when expected_head_sha no longer matches
    the current PR head — a common race with concurrent pushes or CI reruns.
    Rather than fragile keyword-matching on error messages (which breaks silently
    when GitHub rewords responses), we treat any non-success as a potential SHA
    mismatch and always attempt the refetch+retry path once. If the fresh SHA
    equals the original, no retry is made. The extra API call is trivial compared
    to spawning a fix VM. Returns the same {"updated": bool, "message": str} shape.
    """
    try:
        result = await update_branch(
            token, owner, repo, pr_number,
            expected_head_sha=expected_head_sha, project_id=project_id,
        )
    except HTTPException as exc:
        return {"updated": False, "message": f"HTTP error: {exc.detail}"}
    except Exception as exc:
        return {"updated": False, "message": f"{type(exc).__name__}: {exc}"}
    if result.get("updated"):
        return result
    try:
        fresh = await get_pull_request(token, owner, repo, pr_number, project_id=project_id)
    except Exception as exc:
        logger.debug(f"[UPDATE-BRANCH] PR #{pr_number} refetch failed: {exc}")
        return result
    fresh_sha = (fresh.get("head") or {}).get("sha")
    if not fresh_sha or fresh_sha == expected_head_sha:
        return result
    logger.info(
        f"[UPDATE-BRANCH] PR #{pr_number} retrying with fresh head {fresh_sha[:7]} "
        f"(prior failure: {result.get('message')})"
    )
    try:
        return await update_branch(
            token, owner, repo, pr_number,
            expected_head_sha=fresh_sha, project_id=project_id,
        )
    except HTTPException as exc:
        return {"updated": False, "message": f"HTTP error on retry: {exc.detail}"}
    except Exception as exc:
        return {"updated": False, "message": f"{type(exc).__name__} on retry: {exc}"}


async def close_pull_request(
    token: str, owner: str, repo: str, pr_number: int,
    project_id: str | None = None,
) -> bool:
    """Close a PR without merging. Returns True if closed successfully."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "PATCH", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers, json={"state": "closed"}, project_id=project_id,
    )
    if response.status_code == 200:
        logger.info(f"Closed PR #{pr_number} on {owner}/{repo}")
        return True
    logger.warning(f"Failed to close PR #{pr_number}: {response.status_code} {response.text[:200]}")
    return False


async def reopen_pull_request(
    token: str, owner: str, repo: str, pr_number: int,
    project_id: str | None = None,
) -> bool:
    """Reopen a previously closed PR. Returns True on success."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "PATCH", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers, json={"state": "open"}, project_id=project_id,
    )
    if response.status_code == 200:
        logger.info(f"Reopened PR #{pr_number} on {owner}/{repo}")
        return True
    logger.warning(f"Failed to reopen PR #{pr_number}: {response.status_code} {response.text[:200]}")
    return False


async def get_pr_files(
    token: str, owner: str, repo: str, pr_number: int,
    project_id: str | None = None,
) -> list[dict]:
    """Fetches the list of files changed in a PR with their status.

    Raises HTTPException on any non-200 response so callers can distinguish a
    genuinely empty PR (returns []) from a transient GitHub failure. The
    empty-rollup short-circuit in task_lifecycle relies on this distinction to
    avoid closing real PRs when the API is briefly unavailable.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100",
        headers, project_id=project_id,
    )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub authentication failed (401 Unauthorized)")
    if response.status_code != 200:
        logger.error(f"Failed to get PR files #{pr_number}: {response.status_code} {response.text[:300]}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error fetching PR #{pr_number} files (HTTP {response.status_code})",
        )
    return [{"filename": f["filename"], "status": f["status"]} for f in response.json()]


def _unique(*values: int | None) -> list[int]:
    """Return deduplicated non-None values preserving order."""
    seen: set[int] = set()
    result: list[int] = []
    for v in values:
        if v is not None and v not in seen:
            seen.add(v)
            result.append(v)
    return result


async def get_check_runs(
    token: str, owner: str, repo: str, ref: str,
    project_id: str | None = None,
) -> dict:
    """Get CI check run results for a commit SHA.

    Returns:
        {"status": "completed"|"in_progress"|"pending",
         "conclusion": "success"|"failure"|None,
         "details": [{"name": str, "status": str, "conclusion": str|None}],
         "failed_logs": str}
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}/check-runs",
        headers, project_id=project_id,
    )
    if response.status_code != 200:
        logger.error(f"Failed to get check runs for {ref}: {response.text}")
        return {"status": "pending", "conclusion": None, "details": [], "failed_logs": ""}

    data = response.json()
    check_runs = data.get("check_runs", [])

    if not check_runs:
        # No CI configured on this repo — treat as passed so the
        # orchestrator doesn't wait 30 min for checks that will never arrive.
        return {"status": "completed", "conclusion": "success", "details": [], "failed_logs": ""}

    details = []
    failed_names = []
    for cr in check_runs:
        details.append({
            "name": cr.get("name", ""),
            "status": cr.get("status", ""),
            "conclusion": cr.get("conclusion"),
        })
        if cr.get("conclusion") == "failure":
            failed_names.append(cr.get("name", "unknown"))

    # Aggregate status
    statuses = [cr.get("status", "") for cr in check_runs]
    agg_status = "in_progress" if any(s in ("queued", "in_progress") for s in statuses) else "completed"

    # Aggregate conclusion — "skipped" and "neutral" are non-failures
    conclusions = [cr.get("conclusion") for cr in check_runs if cr.get("status") == "completed"]
    if not conclusions:
        agg_conclusion = None
    elif all(c in ("success", "skipped", "neutral") for c in conclusions):
        agg_conclusion = "success"
    else:
        agg_conclusion = "failure"

    # Collect failure details — prefer actual job logs, fall back to annotations
    failed_logs = ""
    if failed_names:
        log_lines = []

        # Pre-fetch actual job IDs via workflow runs API (check run ID != job ID sometimes)
        job_id_by_name: dict[str, int] = {}
        try:
            runs_resp = await _github_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/actions/runs?head_sha={ref}&per_page=5",
                headers, timeout=5.0,
            )
            if runs_resp.status_code == 200:
                wf_runs = runs_resp.json().get("workflow_runs", [])
                jobs_coros = [
                    _github_request(
                        "GET",
                        f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{wf_run['id']}/jobs?filter=latest&per_page=30",
                        headers, timeout=5.0,
                    )
                    for wf_run in wf_runs
                ]
                jobs_results = await asyncio.gather(*jobs_coros, return_exceptions=True)
                for result in jobs_results:
                    if isinstance(result, Exception) or result.status_code != 200:
                        continue
                    for job in result.json().get("jobs", []):
                        if job.get("conclusion") == "failure":
                            job_id_by_name[job.get("name", "")] = job["id"]
        except Exception as exc:
            logger.debug(f"[GITHUB] Workflow runs pre-fetch failed for {ref}: {exc}")

        for cr in check_runs:
            if cr.get("conclusion") != "failure":
                continue
            cr_id = cr.get("id")
            name = cr.get("name", "unknown")

            # Try to fetch actual job logs (check run ID often == job ID)
            got_logs = False
            for job_id in _unique(cr_id, job_id_by_name.get(name)):
                try:
                    log_response = await _github_request(
                        "GET",
                        f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                        headers, follow_redirects=True, timeout=10.0,
                    )
                    if log_response.status_code == 200:
                        raw_log = log_response.text
                        lines = raw_log.strip().split("\n")
                        error_lines = [ln for ln in lines if any(kw in ln.lower() for kw in ["error", "failed", "err!", "✗", "\u00d7"])]
                        if error_lines:
                            log_lines.append(f"  [{name}] Errors:\n" + "\n".join(f"    {ln.strip()}" for ln in error_lines[-30:]))
                        else:
                            log_lines.append(f"  [{name}] (last 30 lines):\n" + "\n".join(f"    {ln.strip()}" for ln in lines[-30:]))
                        got_logs = True
                        break
                except Exception:
                    logger.exception(f"[CI LOGS] Failed to fetch logs for check-run {name}")
            if got_logs:
                continue

            # Fallback: fetch annotations (errors/failures only — skip warnings like deprecation notices)
            try:
                ann_response = await _github_request(
                    "GET",
                    f"https://api.github.com/repos/{owner}/{repo}/check-runs/{cr_id}/annotations",
                    headers
                )
                if ann_response.status_code == 200:
                    for ann in ann_response.json():
                        if ann.get("annotation_level") not in ("failure", "error"):
                            continue
                        msg = ann.get("message", "")
                        path = ann.get("path", "")
                        line = ann.get("start_line", "")
                        log_lines.append(f"  [{name}] {path}:{line} — {msg}")
            except Exception:
                logger.exception(f"[CI LOGS] Failed to fetch annotations for check-run {name}")

            # Also include output summary if available
            output = cr.get("output", {})
            summary = output.get("summary", "") if output else ""
            if summary:
                log_lines.append(f"  [{name}] {summary[:500]}")

        failed_logs = "\n".join(log_lines) if log_lines else "CI checks failed: " + ", ".join(failed_names)

    return {
        "status": agg_status,
        "conclusion": agg_conclusion,
        "details": details,
        "failed_logs": failed_logs,
    }


# ─── Short-lived dedup cache for check-runs lookups ─────────────────────────
# The CI-wait loop in the orchestrator and the check_run webhook handler both
# fetch check-runs per task that's watching a PR.  When N sibling tasks share
# the same head_sha (a rollup with N children is the common case), each task
# was making its own API call — 20 children on one PR = 20 duplicate calls per
# poll interval.  A 30s TTL cache collapses siblings into one call without
# adding any cross-cycle staleness risk beyond the poll interval itself.
_CHECK_RUNS_CACHE_TTL_S = 30
_CHECK_RUNS_CACHE_MAX = 500  # entries; cheap eviction when oversized
_check_runs_cache: dict[tuple[str, str, str], tuple[dict, float]] = {}


def invalidate_check_runs_cache(owner: str, repo: str, head_sha: str) -> None:
    """Drop the cached check-runs result for a SHA — call when the caller
    observes a transition it needs to see immediately (e.g. a webhook arrived
    indicating a new run started).  Safe to call when the key isn't present."""
    _check_runs_cache.pop((owner, repo, head_sha), None)


async def get_check_runs_cached(
    token: str, owner: str, repo: str, head_sha: str,
    *, project_id: str | None = None,
    ttl: float = _CHECK_RUNS_CACHE_TTL_S,
) -> dict:
    """Cached wrapper around ``get_check_runs``.

    TTL is short (30s) so a fresh webhook or a status change is picked up on
    the next orchestrator cycle — the cache exists purely to deduplicate
    simultaneous fan-out from siblings watching the same PR, not to mask
    status changes over time.
    """
    import app.shared_state as _ss  # noqa: PLC0415
    key = (owner, repo, head_sha)
    now = time.time()
    cached = _check_runs_cache.get(key)
    if cached is not None and (now - cached[1]) < ttl:
        _ss.github_cache_hits["check_runs"] = _ss.github_cache_hits.get("check_runs", 0) + 1
        return cached[0]
    result = await get_check_runs(token, owner, repo, head_sha, project_id=project_id)
    _check_runs_cache[key] = (result, now)
    # Lazy eviction: when the dict gets oversized, drop anything older than
    # 2×TTL.  Cheap, avoids unbounded growth across long-lived processes.
    if len(_check_runs_cache) > _CHECK_RUNS_CACHE_MAX:
        cutoff = now - (ttl * 2)
        for k in [k for k, (_, ts) in _check_runs_cache.items() if ts < cutoff]:
            _check_runs_cache.pop(k, None)
    return result
