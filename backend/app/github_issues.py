"""Issue / PR comment / review operations.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

from typing import Any, Dict

from fastapi import HTTPException, status

from app.github import logger


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time so
    monkeypatching ``app.github._github_request`` stays effective here."""
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


async def create_pr_comment(
    token: str, owner: str, repo: str, pr_number: int, body: str
) -> dict:
    """
    Posts a general PR summary or error log back to a GitHub PR.
    Note: GitHub treats PRs as issues for general comments.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload = {"body": body}

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments",
        headers,
        json=payload,
    )

    if response.status_code != 201:
        logger.error(f"Failed to create PR comment: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create PR comment on GitHub.",
        )

    return response.json()


async def add_issue_label(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    label: str,
) -> dict:
    """Apply a label to an issue/PR via GitHub's labels endpoint.

    Idempotent by API contract: posting an already-present label is a no-op
    and still returns success.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"labels": [label]}
    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/labels",
        headers,
        json=payload,
    )
    if response.status_code != 200:
        logger.error(f"Failed to apply issue label '{label}': {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to apply issue label on GitHub.",
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
    body: str,
) -> dict:
    """
    Posts an inline code review comment on a specific line of a file in a PR.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload = {
        "body": body,
        "commit_id": commit_id,
        "path": path,
        "line": line,
        "side": "RIGHT",
    }

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments",
        headers,
        json=payload,
    )

    if response.status_code != 201:
        logger.error(f"Failed to create PR review comment: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create PR review comment on GitHub.",
        )

    return response.json()


async def submit_pr_review(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    event: str,
    body: str,
    comments: list[dict],
) -> dict | None:
    """
    Submits a PR review via the GitHub Pull Request Reviews API.

    Fetches the HEAD commit SHA first, then POSTs the review.
    If inline comments cause a 422, retries without them so a review is always posted.
    Returns None on 404.
    """
    from app import github as _gh

    pr_data = await _gh.get_pull_request(token, owner, repo, pr_number)
    sha = pr_data["head"]["sha"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload: dict = {
        "commit_id": sha,
        "event": event,
        "body": body,
        "comments": comments,
    }

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        headers,
        json=payload,
    )

    if response.status_code == 422:
        logger.warning(
            f"[GITHUB] submit_pr_review 422 for PR #{pr_number} — retrying without inline comments: {response.text[:300]}"
        )
        payload["comments"] = []
        response = await _github_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers,
            json=payload,
        )

    if response.status_code == 404:
        logger.warning(
            f"[GITHUB] submit_pr_review 404 for PR #{pr_number} in {owner}/{repo}"
        )
        return None

    if response.status_code not in (200, 201):
        logger.error(
            f"[GITHUB] Failed to submit PR review #{pr_number}: {response.status_code} {response.text[:300]}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit PR review on GitHub: {response.text[:200]}",
        )

    return response.json()


async def create_issue(
    token: str,
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    assignees: list | None = None,
) -> dict:
    """
    Creates a new issue in the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload: Dict[str, Any] = {"title": title, "body": body}
    if assignees:
        payload["assignees"] = assignees

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers,
        json=payload,
    )

    if response.status_code != 201:
        logger.error(f"Failed to create issue: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create issue on GitHub.",
        )

    return response.json()


async def get_issues(token: str, owner: str, repo: str, state: str = "open") -> list:
    """
    Retrieves a list of issues for the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/issues?state={state}",
        headers,
    )

    if response.status_code != 200:
        logger.error(f"Failed to get issues: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve issues from GitHub.",
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
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
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
            logger.warning(
                f"Failed to get comments for issue #{issue_number}: {response.status_code}"
            )
            return []
        return response.json()
    except Exception as exc:  # noqa: BLE001
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
        logger.error(
            f"Failed to create comment on issue #{issue_number}: {response.text}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create comment on issue #{issue_number}.",
        )

    return response.json()


