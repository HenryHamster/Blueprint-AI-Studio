"""GitHub App JWT / OAuth / installation-token / PAT authentication.

Extracted from ``app.github`` (see that module for the shared low-level
request dispatcher and rate-limit/quota state).  Every symbol here is
re-exported from ``app.github`` so existing ``from app.github import ...``
call sites continue to work unchanged.
"""

import time

import httpx
import jwt
from fastapi import HTTPException, status

from app.github import (
    logger,
    GITHUB_APP_ID,
    GITHUB_PRIVATE_KEY,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
)


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time.

    Routing through the package namespace (rather than binding the function
    at import) keeps ``mocker.patch("app.github._github_request", ...)``
    effective for the functions extracted into this module.
    """
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


def generate_github_app_jwt() -> str:
    """
    Generates a JWT to authenticate as the GitHub App.
    """
    if not GITHUB_APP_ID or not GITHUB_PRIVATE_KEY:
        raise ValueError(
            "GITHUB_APP_ID and GITHUB_PRIVATE_KEY must be set in environment variables."
        )

    now = int(time.time())
    payload = {
        "iat": now
        - 60,  # Issued at time, 60 seconds in the past to allow for clock drift
        "exp": now + (10 * 60),  # JWT expiration time (10 minute maximum)
        "iss": GITHUB_APP_ID,  # GitHub App's identifier
    }

    # Ensure the private key is properly formatted
    private_key = GITHUB_PRIVATE_KEY.replace("\\n", "\n")

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

    from app import github as _gh

    cache_key = f"{owner}/{repo}"
    cached = _installation_id_cache.get(cache_key)
    installation_id: str | None = None
    if cached and time.time() - cached[1] < _INSTALLATION_ID_TTL_SECONDS:
        installation_id = cached[0]

    if installation_id is None:
        app_jwt = _gh.generate_github_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await _github_request(
            "GET", f"https://api.github.com/repos/{owner}/{repo}/installation", headers
        )
        if resp.status_code != 200:
            logger.warning(
                f"No GitHub App installation found for {owner}/{repo}: {resp.status_code}"
            )
            _installation_id_cache.pop(cache_key, None)
            return None
        installation_id = str(resp.json()["id"])
        _installation_id_cache[cache_key] = (installation_id, time.time())

    return await _gh.generate_installation_access_token(
        installation_id, repositories=[repo]
    )


async def exchange_oauth_code(code: str, redirect_uri: str | None = None) -> dict:
    """Exchange a GitHub OAuth code for a user access token."""
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise ValueError("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET must be set")

    body: dict[str, str] = {
        "client_id": GITHUB_CLIENT_ID,
        "client_secret": GITHUB_CLIENT_SECRET,
        "code": code,
    }
    if redirect_uri is not None:
        body["redirect_uri"] = redirect_uri

    resp = await _github_request(
        "POST",
        "https://github.com/login/oauth/access_token",
        {"Accept": "application/json"},
        json=body,
    )
    if resp.status_code != 200:
        logger.error(
            "[GITHUB OAUTH] Token exchange returned %s: %s",
            resp.status_code,
            resp.text,
        )
        raise HTTPException(status_code=502, detail="GitHub OAuth exchange failed")

    token_response = resp.json()
    err_code = token_response.get("error")
    if err_code:
        logger.error("[GITHUB OAUTH] Token endpoint error payload: %s", token_response)
        err_msg = token_response.get("error_description") or err_code
        raise HTTPException(status_code=400, detail=f"GitHub OAuth error: {err_msg}")
    return token_response


async def get_app_info() -> dict:
    """Return metadata about this GitHub App from /app."""
    if not GITHUB_APP_ID or not GITHUB_PRIVATE_KEY:
        raise ValueError("GITHUB_APP_ID and GITHUB_PRIVATE_KEY must be set")

    from app import github as _gh

    auth_jwt = _gh.generate_github_app_jwt()
    api_headers: dict[str, str] = {
        "Authorization": f"Bearer {auth_jwt}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = await _github_request("GET", "https://api.github.com/app", api_headers)
    if resp.status_code != 200:
        logger.error("Could not retrieve GitHub App metadata: %s", resp.text)
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"GitHub API error: {resp.text}",
        )
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
            raise HTTPException(
                status_code=resp.status_code, detail=f"GitHub API error: {resp.text}"
            )
        data = resp.json()
        installations.extend(data.get("installations", []))
        # Follow pagination
        url = resp.links.get("next", {}).get("url")
    return installations


async def get_github_user(user_token: str) -> dict:
    """Get the authenticated GitHub user's profile."""
    resp = await _github_request(
        "GET",
        "https://api.github.com/user",
        {
            "Authorization": f"Bearer {user_token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail="Failed to fetch GitHub user profile"
        )
    return resp.json()


async def generate_installation_access_token(
    installation_id: str, repositories: list[str] | None = None
) -> str:
    """
    Exchanges the GitHub App JWT for a short-lived Installation Access Token.
    """
    app_jwt = generate_github_app_jwt()

    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload = {}
    if repositories:
        payload["repositories"] = repositories

    try:
        response = await _github_request(
            "POST",
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers,
            json=payload,
        )
    except httpx.HTTPError as e:
        logger.exception("Network error generating installation token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to reach GitHub API: {e}",
        )

    if response.status_code != 201:
        logger.error(f"Failed to generate installation access token: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to authenticate with GitHub App ({response.status_code}): {response.text[:200]}",
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
            detail="Invalid token format. Please provide a Fine-Grained Personal Access Token (starts with 'github_pat_'). Classic tokens ('ghp_') are not allowed for security reasons.",
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. Check if the token is valid by hitting the authenticated user endpoint
    user_response = await _github_request("GET", "https://api.github.com/user", headers)

    if user_response.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The provided GitHub token is invalid or expired.",
        )
    elif user_response.status_code != 200:
        logger.error(f"GitHub API error: {user_response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate token with GitHub.",
        )

    # 2. If a target repo is provided, verify the token can access it
    if target_repo:
        repo_response = await _github_request(
            "GET", f"https://api.github.com/repos/{target_repo}", headers
        )
        if repo_response.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"The token does not have access to the repository '{target_repo}'. Ensure the Fine-Grained PAT is scoped to this repository.",
            )
        elif repo_response.status_code != 200:
            logger.error(f"GitHub API error on repo check: {repo_response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to verify repository access with GitHub.",
            )

        # Optionally check if the token has push access
        repo_data = repo_response.json()
        permissions = repo_data.get("permissions", {})
        if not permissions.get("push", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"The token has read access to '{target_repo}', but lacks 'push' (write) permissions. Please update the token scopes.",
            )

    return {
        "status": "valid",
        "message": "Fine-Grained PAT is valid and properly scoped.",
        "user": user_response.json().get("login"),
    }


