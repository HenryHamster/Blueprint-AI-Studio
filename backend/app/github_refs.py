"""Branch / tag / commit-metadata / PR-discovery / ancestry operations.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

from datetime import datetime

from fastapi import HTTPException, status

from app.github import logger

# Keep `datetime` referenced so linters don't strip it; it is only used as a
# forward reference in `get_commit_date(...) -> "datetime | None"`.
_DATETIME = datetime


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time so
    monkeypatching ``app.github._github_request`` stays effective here."""
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


async def create_branch(
    token: str, owner: str, repo: str, branch_name: str, sha: str
) -> dict:
    """
    Creates a new branch (git reference) in the specified repository.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload = {"ref": f"refs/heads/{branch_name}", "sha": sha}

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        headers,
        json=payload,
    )

    if response.status_code == 422:
        # Branch may already exist (e.g., retry after 502 that actually succeeded)
        from app import github as _gh

        try:
            existing_sha = await _gh.get_branch_sha(token, owner, repo, branch_name)
            logger.info(
                f"Branch '{branch_name}' already exists (sha: {existing_sha[:8]}), reusing"
            )
            return {"ref": f"refs/heads/{branch_name}", "object": {"sha": existing_sha}}
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Branch '{branch_name}' 422 but get_branch_sha also failed; falling through"
            )

    if response.status_code != 201:
        logger.error(f"Failed to create branch: {response.status_code} {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create branch '{branch_name}' on GitHub (GitHub {response.status_code}).",
        )

    return response.json()


async def create_tag(
    token: str,
    owner: str,
    repo: str,
    tag: str,
    sha: str,
    message: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Create an annotated git tag in the specified repository.

    Two-step: first creates the annotated tag object via POST git/tags, then
    creates the ref ``refs/tags/{tag}`` pointing at the tag object SHA.

    Returns the created ref object dict on success.  Raises HTTPException on
    API errors so callers can choose to wrap in try/except for best-effort use.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    from app.time_utils import _utcnow as _ut

    tag_message = message or f"Release {tag}"

    # Step 1: create the annotated tag object.
    tag_payload = {
        "tag": tag,
        "message": tag_message,
        "object": sha,
        "type": "commit",
        "tagger": {
            "name": "Blueprint",
            "email": "blueprint@users.noreply.github.com",
            "date": _ut().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    tag_resp = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/tags",
        headers,
        json=tag_payload,
        project_id=project_id,
    )
    if tag_resp.status_code != 201:
        logger.error(
            f"[GITHUB] create_tag: failed to create tag object '{tag}': "
            f"{tag_resp.status_code} {tag_resp.text[:300]}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Failed to create tag object '{tag}' on GitHub "
                f"(GitHub {tag_resp.status_code})."
            ),
        )
    tag_object_sha = tag_resp.json()["sha"]

    # Step 2: create the ref refs/tags/{tag} pointing at the tag object.
    ref_payload = {"ref": f"refs/tags/{tag}", "sha": tag_object_sha}
    ref_resp = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        headers,
        json=ref_payload,
        project_id=project_id,
    )
    if ref_resp.status_code == 422:
        # Tag ref may already exist (idempotent re-tag on retry).
        logger.info(
            f"[GITHUB] create_tag: ref refs/tags/{tag} already exists (422) — reusing"
        )
        return {"ref": f"refs/tags/{tag}", "object": {"sha": tag_object_sha}}

    if ref_resp.status_code != 201:
        logger.error(
            f"[GITHUB] create_tag: failed to create ref refs/tags/{tag}: "
            f"{ref_resp.status_code} {ref_resp.text[:300]}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Failed to create tag ref 'refs/tags/{tag}' on GitHub "
                f"(GitHub {ref_resp.status_code})."
            ),
        )

    return ref_resp.json()


async def get_branch_sha(token: str, owner: str, repo: str, branch: str) -> str:
    """Gets the latest commit SHA of a branch."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
        headers,
    )
    if response.status_code == 404:
        logger.warning(f"Branch '{branch}' not found (404)")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Branch '{branch}' not found (GitHub 404).",
        )
    if response.status_code != 200:
        logger.error(
            f"Failed to get SHA for branch {branch}: {response.status_code} {response.text}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error getting branch '{branch}' SHA (HTTP {response.status_code}).",
        )
    return response.json()["object"]["sha"]


async def get_repo_default_branch(token: str, owner: str, repo: str) -> str:
    """Get the repository's default branch name (usually 'main' or 'master')."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET", f"https://api.github.com/repos/{owner}/{repo}", headers
    )
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error detecting default branch (HTTP {response.status_code}).",
        )
    data = response.json()
    if "default_branch" not in data:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub API response missing default_branch field.",
        )
    return data["default_branch"]


async def delete_branch(token: str, owner: str, repo: str, branch: str) -> bool:
    """Deletes a branch (git reference). Returns True if deleted."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "DELETE",
        f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
        headers,
    )
    if response.status_code == 204:
        return True
    logger.warning(f"Failed to delete branch {branch}: {response.status_code}")
    return False


async def list_branches(
    token: str,
    owner: str,
    repo: str,
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
            raise HTTPException(
                status_code=401,
                detail="GitHub authentication failed (401 Unauthorized)",
            )
        if response.status_code != 200:
            logger.error(
                f"[GITHUB] Failed to list branches for {owner}/{repo}: {response.status_code}"
            )
            return []
        data = response.json()
        for b in data:
            name = b["name"]
            if prefixes is None or any(name.startswith(p) for p in prefixes):
                branches.append({"name": name, "sha": b["commit"]["sha"]})
        url = response.links.get("next", {}).get("url")

    return branches


# A commit's committer-date is immutable for a given SHA, so this is safe to
# cache for the process lifetime keyed by (owner, repo, sha). The branch-cleanup
# sweep calls get_commit_date once per terminal branch every cycle; with a large
# backlog of un-deletable orphan branches (e.g. the deleted-project cascade) the
# same ~300 SHAs were re-fetched every sweep, producing ~1.3k redundant
# GET /commits/{sha} calls in 2h and dominating the shared GitHub rate budget
# (incident 2026-05-30). The cache collapses repeat sweeps to ~0 GitHub calls.
# Bounded so a long-lived process can't grow it without limit; misses (None) are
# NOT cached so a transient API error retries next cycle.
_COMMIT_DATE_CACHE: "dict[tuple[str, str, str], datetime]" = {}
_COMMIT_DATE_CACHE_MAX = 5000


async def get_commit_date(
    token: str, owner: str, repo: str, sha: str
) -> "datetime | None":
    """Get the committer date for a commit SHA. Returns None on error.

    Cached by (owner, repo, sha) — commit dates are immutable, so a hit never
    goes stale. Only successful lookups are cached; errors fall through to a
    fresh fetch next call.
    """
    from datetime import datetime as _dt

    cache_key = (owner, repo, sha)
    cached = _COMMIT_DATE_CACHE.get(cache_key)
    if cached is not None:
        return cached

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
        parsed = _dt.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Bounded insert: clear wholesale when oversized (cheap; entries are
    # immutable so dropping them only costs a re-fetch, not correctness).
    if len(_COMMIT_DATE_CACHE) >= _COMMIT_DATE_CACHE_MAX:
        _COMMIT_DATE_CACHE.clear()
    _COMMIT_DATE_CACHE[cache_key] = parsed
    return parsed


# Short-lived dedup cache for list_pull_requests_for_commit.
# The ci_resolution fallback path calls this once per no-checks head SHA; when
# N sibling tasks share the same branch_sha (common in rollups), each task was
# issuing a redundant GET commits/{sha}/pulls.  A 30 s TTL matches the
# check-runs cache so both dedup windows align.  Error results (empty list from
# a non-200 response) are NOT cached so the next call retries the real lookup.
_LIST_PRS_FOR_COMMIT_CACHE_TTL_S = 30
_LIST_PRS_FOR_COMMIT_CACHE_MAX = 500
_list_prs_for_commit_cache: "dict[tuple[str, str, str], tuple[list[dict], float]]" = {}


async def list_pull_requests_for_commit(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    project_id: str | None = None,
) -> list[dict]:
    """List the pull request(s) associated with a commit SHA.

    Wraps ``GET /repos/{owner}/{repo}/commits/{sha}/pulls`` — for a commit on a
    default / integration branch this returns the merged PR that introduced it,
    plus any open PR whose head is the commit.

    The release qualifier uses this to find the PR head commit where
    ``pull_request``-triggered CI actually ran: a freshly-merged ``automain``
    HEAD commit carries no check-runs of its own because CI runs only on
    ``pull_request`` events.

    Returns the raw list of PR dicts (each carries ``number``, ``state``,
    ``merged_at``, ``head``, ``base``).  Returns ``[]`` on any non-200 response
    or unparseable body — graceful degradation, never raises for an API error.

    Results are cached for 30 s keyed by (owner, repo, sha) to deduplicate
    simultaneous fan-out from sibling tasks watching the same branch.  Error
    results (empty list from non-200) are NOT cached so the next call retries.
    """
    import time as _time

    cache_key = (owner, repo, sha)
    now = _time.time()
    cached = _list_prs_for_commit_cache.get(cache_key)
    if cached is not None and (now - cached[1]) < _LIST_PRS_FOR_COMMIT_CACHE_TTL_S:
        return cached[0]

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    all_prs: list[dict] = []
    url: str | None = (
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/pulls?per_page=100"
    )
    while url:
        response = await _github_request(
            "GET",
            url,
            headers,
            project_id=project_id,
        )
        if response.status_code != 200:
            logger.warning(
                "[GITHUB] list_pull_requests_for_commit: %s/%s commit %s returned %s",
                owner,
                repo,
                sha[:8] if sha else sha,
                response.status_code,
            )
            # Do not cache error results — callers should retry on the next
            # cycle rather than serve a stale empty list.
            return []
        try:
            page = response.json()
        except (ValueError, TypeError):
            logger.warning(
                "[GITHUB] list_pull_requests_for_commit: unparseable body for "
                "%s/%s commit %s",
                owner,
                repo,
                sha[:8] if sha else sha,
            )
            return []
        if not isinstance(page, list):
            return []
        all_prs.extend(page)
        url = response.links.get("next", {}).get("url")

    # Cache successful (possibly empty-list-from-no-PRs) results.
    _list_prs_for_commit_cache[cache_key] = (all_prs, now)
    if len(_list_prs_for_commit_cache) > _LIST_PRS_FOR_COMMIT_CACHE_MAX:
        cutoff = now - (_LIST_PRS_FOR_COMMIT_CACHE_TTL_S * 2)
        for k in [
            k
            for k, (_, ts) in _list_prs_for_commit_cache.items()
            if ts < cutoff
        ]:
            _list_prs_for_commit_cache.pop(k, None)
    return all_prs


async def is_ancestor_of(
    token: str,
    owner: str,
    repo: str,
    ancestor_sha: str,
    descendant_ref: str,
    project_id: str | None = None,
) -> bool | None:
    """Return True iff ``ancestor_sha`` is reachable from / merged into ``descendant_ref``.

    Read-only helper built on GitHub's compare API
    (``GET /repos/{owner}/{repo}/compare/{base}...{head}``).  Comparing
    ``ancestor_sha...descendant_ref`` returns a ``status`` field:

      * ``"identical"`` — the two refs point at the same commit.
      * ``"ahead"``     — ``descendant_ref`` is strictly ahead of
        ``ancestor_sha`` (every commit in ``ancestor_sha`` is also in
        ``descendant_ref``).
      * ``"behind"`` / ``"diverged"`` — ``descendant_ref`` does NOT contain
        ``ancestor_sha``.

    So ``ancestor_sha`` is an ancestor of (or equal to) ``descendant_ref`` iff
    the status is ``"identical"`` or ``"ahead"``.

    This is the idempotency primitive for the release qualifier's ``promote()``
    — it answers "has this exact candidate commit already landed on main?"
    after a crash/retry, so a partially-completed promotion is not re-merged or
    misclassified as a failure.

    Returns ``True``/``False`` on a definitive answer, or ``None`` when the
    comparison could not be made (API error, ref not found) so the caller can
    decide how to degrade — ``None`` deliberately is NOT coerced to a bool, so
    a transient failure never silently looks like "already merged".
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/compare/"
        f"{ancestor_sha}...{descendant_ref}",
        headers,
        project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(
            "[GITHUB] is_ancestor_of: compare %s...%s for %s/%s returned %s — "
            "cannot determine ancestry",
            ancestor_sha[:8] if ancestor_sha else ancestor_sha,
            descendant_ref,
            owner,
            repo,
            response.status_code,
        )
        return None
    try:
        compare_status = response.json().get("status")
    except (ValueError, TypeError) as exc:
        logger.warning(
            "[GITHUB] is_ancestor_of: unparseable compare response for %s/%s: %s",
            owner,
            repo,
            exc,
        )
        return None
    # "identical" (same commit) or "ahead" (descendant strictly contains the
    # ancestor) both mean ancestor_sha is reachable from descendant_ref.
    return compare_status in ("identical", "ahead")


async def get_files_changed_between_refs(
    token: str,
    owner: str,
    repo: str,
    base_ref: str,
    head_ref: str,
    project_id: str | None = None,
) -> list[str] | None:
    """Return filenames that differ between ``base_ref`` and ``head_ref``.

    Uses ``GET /repos/{owner}/{repo}/compare/{base}...{head}`` and extracts
    the ``files[].filename`` list.  This is the content-aware landing check
    for the rollup-squash case: when a task's original commit SHA is not an
    ancestor of automain (because rollup re-applied the content under a new
    squashed commit), comparing the two branch tips answers "are these files
    still different?" — if the fix's target files are NOT in the diff, the
    fix landed.

    Returns ``None`` on API error so callers can treat indeterminate results
    as such rather than silently assuming "not changed" or "changed".
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/compare/{base_ref}...{head_ref}",
        headers,
        project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(
            "[GITHUB] get_files_changed_between_refs: compare %s...%s for %s/%s "
            "returned %s — cannot determine changed files",
            base_ref,
            head_ref,
            owner,
            repo,
            response.status_code,
        )
        return None
    try:
        files = response.json().get("files", [])
        return [f["filename"] for f in files if "filename" in f]
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning(
            "[GITHUB] get_files_changed_between_refs: unparseable compare response "
            "for %s/%s: %s",
            owner,
            repo,
            exc,
        )
        return None


