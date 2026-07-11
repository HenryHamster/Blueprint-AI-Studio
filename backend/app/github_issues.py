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


def _is_self_review_422(response_text: str) -> bool:
    """True when a 422 from the Reviews API is GitHub rejecting an
    APPROVE / REQUEST_CHANGES review on a PR authored by the same identity
    as the reviewing token.

    GitHub only permits ``COMMENT`` reviews on your own PR; APPROVE and
    REQUEST_CHANGES return 422 with a message like
    "Can not approve your own pull request" /
    "Can not request changes on your own pull request". This is the common
    case for the autonomous pipeline today, where every PR is authored by the
    single Blueprint GitHub-App bot and reviewed with that same token.
    """
    if not response_text:
        return False
    return "your own pull request" in response_text.lower()


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

    Handles the two distinct 422 causes so a review is always posted and no
    futile retries are wasted. GitHub reports only ONE 422 cause per response,
    so the two causes can surface in EITHER order on the same review and must
    compose — each is remediated at most once (max 3 POSTs total):

    * **Self-authored review** — GitHub forbids APPROVE / REQUEST_CHANGES on a
      PR you authored (only COMMENT is allowed). The verdict already lives in
      ``body``, so we downgrade the event to ``COMMENT`` and retry, keeping any
      inline comments. APPROVE / REQUEST_CHANGES are preserved when the PR
      author differs from the reviewing identity (future separate-reviewer or
      external-contributor PRs).
    * **Bad inline comments** — a comment anchored to a path/line outside the
      diff. We drop the inline comments and retry, keeping the current event.

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

    async def _post():
        # Send a shallow copy so each POST carries its own snapshot of the
        # payload (event / comments get mutated across retries below).
        return await _github_request(
            "POST",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers,
            json=dict(payload),
        )

    response = await _post()

    # GitHub reports one 422 cause at a time, so a comments-422 on the first
    # POST can mask a self-review-422 (and vice versa) — seen in production as
    # "Path could not be resolved" followed by "Can not request changes on
    # your own pull request". Remediate each cause at most once, in whatever
    # order GitHub surfaces them: up to 2 remediation retries (3 POSTs total).
    event_downgraded = False
    for _ in range(2):
        if response.status_code != 422:
            break
        if _is_self_review_422(response.text) and not event_downgraded:
            # Self-authored PR: GitHub rejects APPROVE/REQUEST_CHANGES. The
            # verdict is already in the review body — submit it as a COMMENT
            # review instead.
            logger.info(
                f"[GITHUB] submit_pr_review 422 for PR #{pr_number} — self-authored, "
                f"downgrading event {event!r} -> 'COMMENT' and retrying"
            )
            payload["event"] = "COMMENT"
            event_downgraded = True
        elif payload["comments"]:
            # Other 422 cause: inline comments anchored outside the diff.
            # Retry without them so a review is still posted.
            logger.warning(
                f"[GITHUB] submit_pr_review 422 for PR #{pr_number} — retrying without inline comments: {response.text[:300]}"
            )
            payload["comments"] = []
        else:
            # Both remediations exhausted (or inapplicable) — fall through to
            # the generic non-2xx handling below.
            break
        response = await _post()

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


