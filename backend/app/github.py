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

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/installation",
            headers=headers
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

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET, "code": code},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="GitHub OAuth exchange failed")
        data = resp.json()
        if "error" in data:
            raise HTTPException(status_code=400, detail=f"GitHub OAuth error: {data.get('error_description', data['error'])}")
        return data


async def get_user_installations(user_token: str) -> list[dict]:
    """Fetch the GitHub App installations accessible to the authenticated user."""
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    installations = []
    async with httpx.AsyncClient() as client:
        url = "https://api.github.com/user/installations?per_page=100"
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch user installations: {resp.text}")
                break
            data = resp.json()
            installations.extend(data.get("installations", []))
            # Follow pagination
            url = resp.links.get("next", {}).get("url")
    return installations


async def get_github_user(user_token: str) -> dict:
    """Get the authenticated GitHub user's profile."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {user_token}", "Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch GitHub user profile")
        return resp.json()


async def generate_installation_access_token(installation_id: str, repositories: list[str] = None) -> str:
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
        
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers=headers,
            json=payload
        )
        
        if response.status_code != 201:
            logger.error(f"Failed to generate installation access token: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to authenticate with GitHub App."
            )
            
        return response.json().get("token")

async def validate_fine_grained_pat(token: str, target_repo: str = None) -> dict:
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

    async with httpx.AsyncClient() as client:
        # 1. Check if the token is valid by hitting the authenticated user endpoint
        user_response = await client.get("https://api.github.com/user", headers=headers)
        
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
            repo_response = await client.get(f"https://api.github.com/repos/{target_repo}", headers=headers)
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
        
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/statuses/{sha}",
            headers=headers,
            json=payload
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
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=headers,
            json=payload
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
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            headers=headers,
            json=payload
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
    assignees: list = None
) -> dict:
    """
    Creates a new issue in the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    payload = {
        "title": title,
        "body": body
    }
    if assignees:
        payload["assignees"] = assignees
        
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            headers=headers,
            json=payload
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
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues?state={state}",
            headers=headers
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
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/git/refs",
            headers=headers,
            json=payload
        )
        
        if response.status_code != 201:
            logger.error(f"Failed to create branch: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create branch '{branch_name}' on GitHub."
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
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
            headers=headers
        )
        if response.status_code != 200:
            logger.error(f"Failed to get SHA for branch {branch}: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get branch '{branch}' SHA."
            )
        return response.json()["object"]["sha"]


async def delete_branch(
    token: str, owner: str, repo: str, branch: str
) -> bool:
    """Deletes a branch (git reference). Returns True if deleted."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(
            f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=headers
        )
        if response.status_code == 204:
            return True
        logger.warning(f"Failed to delete branch {branch}: {response.status_code}")
        return False


async def create_pull_request(
    token: str,
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = ""
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
        "title": title,
        "head": head,
        "base": base,
        "body": body
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=headers,
            json=payload
        )

        if response.status_code != 201:
            logger.error(f"Failed to create pull request: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create pull request on GitHub."
            )

        return response.json()


async def get_pull_request(
    token: str, owner: str, repo: str, pr_number: int
) -> dict:
    """Fetches PR details."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers
        )
        if response.status_code != 200:
            logger.error(f"Failed to get PR #{pr_number}: {response.text}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get PR #{pr_number}")
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
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers
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
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits?per_page=100",
            headers=headers
        )
        if response.status_code != 200:
            logger.error(f"Failed to get PR commits #{pr_number}: {response.text}")
            return []
        return response.json()


async def merge_pull_request(
    token: str, owner: str, repo: str, pr_number: int, merge_method: str = "squash"
) -> dict:
    """Merges a PR. Returns dict with 'merged' bool and 'message'. Does not raise on conflict."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            headers=headers,
            json={"merge_method": merge_method}
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
