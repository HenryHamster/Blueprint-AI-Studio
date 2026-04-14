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
    for attempt in range(max_retries):
        try:
            client = _get_client()
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

async def get_repo_installation_token(owner: str, repo: str) -> str | None:
    """
    Discovers the GitHub App installation for a specific repo and returns an access token.
    Used as a fallback when no UserIntegration exists (e.g., admin-created tasks).
    Returns None if the App is not installed on the repo.
    """
    if not GITHUB_APP_ID or not GITHUB_PRIVATE_KEY:
        return None

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
        return None
    installation_id = str(resp.json()["id"])

    return await generate_installation_access_token(installation_id, repositories=[repo])


async def exchange_oauth_code(code: str) -> dict:
    """Exchange a GitHub OAuth code for a user access token."""
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise ValueError("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET must be set")

    payload = {"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET, "code": code}

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
        logger.error(f"Network error generating installation token: {e}")
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
            pass  # fall through to error below

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
    token: str, owner: str, repo: str, base: str
) -> list[dict]:
    """Fetches open PRs targeting a specific base branch."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers, params={"state": "open", "base": base, "per_page": 100}
    )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub authentication failed (401 Unauthorized)")
    if response.status_code != 200:
        logger.error(f"Failed to list PRs for base={base}: {response.text}")
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
    logger.error(f"Failed to merge PR #{pr_number}: {response.text}")
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to merge PR #{pr_number}")


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


async def get_pr_files(
    token: str, owner: str, repo: str, pr_number: int,
    project_id: str | None = None,
) -> list[dict]:
    """Fetches the list of files changed in a PR with their status (added/modified/removed)."""
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
        logger.error(f"Failed to get PR files #{pr_number}: {response.text}")
        return []
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
                    pass
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
                pass

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
