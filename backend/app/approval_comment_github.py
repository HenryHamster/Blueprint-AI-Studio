"""Strict GitHub transport for recoverable approval-merge comments."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _next_link(raw_link: str, *, expected_path: str) -> str | None:
    for part in raw_link.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^";]+)"?', part)
        if match is None or match.group(2) != "next":
            continue
        candidate = match.group(1)
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path != expected_path
        ):
            raise RuntimeError("approval comment lookup failed: invalid next link")
        return candidate
    return None


async def approval_comment_marker_exists_strict(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    marker: str,
    *,
    project_id: str,
) -> bool:
    """Search every GitHub comment page, raising on any incomplete lookup."""

    from app import github

    path = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
    url: str | None = f"https://api.github.com{path}?per_page=100"
    seen: set[str] = set()
    while url is not None:
        if url in seen:
            raise RuntimeError("approval comment lookup failed: pagination cycle")
        seen.add(url)
        response = await github._github_request(
            "GET",
            url,
            _headers(token),
            project_id=project_id,
            consumer="approval_merge_comment_recovery",
        )
        if response.status_code != 200:
            raise RuntimeError(
                "approval comment lookup failed: "
                f"GitHub returned {response.status_code}"
            )
        try:
            comments = response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "approval comment lookup failed: invalid JSON response"
            ) from exc
        if not isinstance(comments, list):
            raise RuntimeError(
                "approval comment lookup failed: response was not a list"
            )
        for comment in comments:
            if not isinstance(comment, dict):
                raise RuntimeError(
                    "approval comment lookup failed: malformed comment item"
                )
            body = comment.get("body")
            if not isinstance(body, str):
                raise RuntimeError(
                    "approval comment lookup failed: malformed comment body"
                )
            if marker in body:
                return True
        raw_link = response.headers.get("Link") or response.headers.get("link") or ""
        url = _next_link(str(raw_link), expected_path=path) if raw_link else None
    return False


async def create_approval_comment_once(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    *,
    project_id: str,
) -> dict:
    """Submit once; transport ambiguity is reconciled by a later marker GET."""

    from app import github

    response = await github._github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
        _headers(token),
        max_retries=1,
        project_id=project_id,
        consumer="approval_merge_comment_delivery",
        json={"body": body},
    )
    if response.status_code != 201:
        raise RuntimeError(
            f"approval comment POST failed: GitHub returned {response.status_code}"
        )
    try:
        result = response.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("approval comment POST returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("approval comment POST returned a non-object response")
    return result
