"""Pull-request lifecycle mutations plus read-only PR-state / ancestry checks.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

from fastapi import HTTPException, status

from app.github import logger


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time so
    monkeypatching ``app.github._github_request`` stays effective here."""
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


async def pr_is_not_behind(
    token: str,
    owner: str,
    repo: str,
    *,
    head_sha: str | None = None,
    base_tip_sha: str | None = None,
    mergeable_state: str | None = None,
    project_id: str | None = None,
    call_site: str | None = None,
) -> bool | None:
    """Return True iff a PR is NOT behind its base branch.

    Two signals are checked in order (short-circuit on first conclusive result):

    1. ``mergeable_state == "clean"`` — GitHub's authoritative signal.  A clean
       PR is not behind regardless of SHA comparison.
    2. Ancestry check — if ``base_tip_sha`` is already reachable from
       ``head_sha`` the PR head already contains the base tip, so it is not
       behind even when the two SHAs differ and GitHub has not yet recomputed
       mergeable_state.

    Returns ``True`` (not behind — safe to attempt merge without update-branch),
    ``False`` (behind — base tip not yet in PR head), or ``None`` (indeterminate:
    API error or insufficient information provided).

    When ``call_site`` is provided, the outcome (True/False/None) is recorded
    via ``shared_state.record_pr_is_not_behind`` so the admin diagnostics
    endpoint can surface short-circuit hit rates. Call-site labels follow a
    ``<subsystem>[_<sub-phase>]`` convention — see
    ``shared_state.PR_IS_NOT_BEHIND_CALL_SITES`` for the canonical set.
    """
    from app.shared_state import record_pr_is_not_behind
    from app import github as _gh

    if (mergeable_state or "").lower() == "clean":
        if call_site:
            record_pr_is_not_behind(call_site, True)
        return True

    if head_sha and base_tip_sha:
        ancestor = await _gh.is_ancestor_of(
            token, owner, repo, base_tip_sha, head_sha, project_id=project_id
        )
        if ancestor is True:
            if call_site:
                record_pr_is_not_behind(call_site, True)
            return True
        if ancestor is False:
            if call_site:
                record_pr_is_not_behind(call_site, False)
            return False
        # ancestor is None (API error) — fall through to mergeable_state signal

    if (mergeable_state or "").lower() == "behind":
        if call_site:
            record_pr_is_not_behind(call_site, False)
        return False

    if call_site:
        record_pr_is_not_behind(call_site, None)
    return None


async def pr_is_not_behind_for(
    token: str,
    owner: str,
    repo: str,
    *,
    pr_detail: dict | None,
    base_branch: str | None = None,
    base_tip_sha: str | None = None,
    project_id: str | None = None,
    call_site: str | None = None,
) -> bool | None:
    """Convenience wrapper around :func:`pr_is_not_behind` that pulls
    ``head_sha`` and ``mergeable_state`` from a PR-detail dict and (optionally)
    fetches the base branch's tip SHA via :func:`get_branch_sha`.

    Every behind-recovery caller of ``update_branch`` / ``update_branch_with_sha_retry``
    needs to consult :func:`pr_is_not_behind` first. The orchestration boilerplate
    around that consultation is identical across callers — extract head SHA from
    the PR dict, fetch the base tip with a defensive try/except, then forward
    the params. This helper collapses that boilerplate to a single ``await``.

    Pass an already-fetched ``base_tip_sha`` to skip the GitHub round-trip when
    the caller has it in hand (e.g. the rollup pre-merge site needs the tip SHA
    independently for its own comparison).
    """
    from app import github as _gh

    detail = pr_detail or {}
    head_sha = (detail.get("head") or {}).get("sha")
    mergeable_state = detail.get("mergeable_state") or ""
    if base_tip_sha is None and head_sha and base_branch:
        try:
            base_tip_sha = await _gh.get_branch_sha(token, owner, repo, base_branch)
        except Exception as exc:  # noqa: BLE001 — mirrors caller-site behavior: any get_branch_sha failure (network, 404, auth) must fall through to pr_is_not_behind with base_tip_sha=None so it can decide based on mergeable_state alone.
            logger.warning(
                f"[PR_IS_NOT_BEHIND] get_branch_sha({base_branch}) failed for "
                f"{owner}/{repo}: {exc}"
            )
    return await _gh.pr_is_not_behind(
        token,
        owner,
        repo,
        head_sha=head_sha,
        base_tip_sha=base_tip_sha,
        mergeable_state=mergeable_state,
        project_id=project_id,
        call_site=call_site,
    )


async def _find_open_pull_request(
    headers: dict,
    owner: str,
    repo: str,
    head: str,
    base: str,
    project_id: str | None = None,
) -> dict | None:
    """Return the open PR for ``head`` -> ``base`` if one exists, else None.

    GitHub allows at most one open PR per (head, base) pair but enforces it only
    best-effort — two concurrent ``POST /pulls`` can both succeed (201) and leave
    a duplicate (rollup PR incident #7350/#7351). This lookup lets
    ``create_pull_request`` collapse the "already created" case into resuming the
    existing PR. Best-effort by design: any non-200 response or error returns
    None so a flaky lookup never blocks creation — the POST and its 422 handler
    still cover the race.
    """
    try:
        list_resp = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls"
            f"?head={owner}:{head}&base={base}&state=open",
            headers,
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 — any lookup failure (network, auth, HTTP) must fall through to the POST so a flaky pre-check never blocks creation.
        logger.warning(f"[PR] Pre-create lookup for {head} -> {base} failed: {exc}")
        return None
    if getattr(list_resp, "status_code", None) == 200:
        try:
            items = list_resp.json()
        except Exception:  # noqa: BLE001 — a malformed body is treated as "no existing PR" so creation proceeds.
            return None
        if items:
            return items[0]
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
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Idempotency pre-check: GitHub enforces one open PR per (head, base) only
    # best-effort, so two concurrent creates can both 201 → duplicate PRs
    # (incident #7350/#7351). Resume an already-open PR instead of POSTing again.
    existing = await _find_open_pull_request(
        headers, owner, repo, head, base, project_id
    )
    if existing is not None:
        logger.info(
            f"[PR] Open PR #{existing.get('number')} already exists for "
            f"{head} -> {base} — returning it instead of creating a duplicate"
        )
        return existing

    payload = {"title": title[:256], "head": head, "base": base, "body": body[:65536]}

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers,
        json=payload,
        project_id=project_id,
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
                logger.info(
                    f"PR already exists for {head} -> {base}, fetching existing PR"
                )
                existing = await _find_open_pull_request(
                    headers, owner, repo, head, base, project_id
                )
                if existing is not None:
                    return existing
            if "no commits between" in msg:
                # Empty diff — branches are identical. Return a synthetic "merged" result
                # so the caller can mark the task as completed instead of failed.
                logger.info(f"[PR] No commits between {head} and {base} — empty diff")
                return {"empty_diff": True, "merged": True, "html_url": "", "number": 0}
        # Unmatched 422 (e.g. "head ref missing", validation errors). Surface the
        # real GitHub message with 422 so the caller can distinguish upstream
        # validation failures from internal server errors. Detail starts with
        # "Failed to create pull request" so the EXTERNAL_API classifier still
        # routes it correctly.
        gh_msg = data.get("message", "Validation failed")
        err_msgs = "; ".join(
            str(e.get("message", "")) for e in errors if e.get("message")
        )
        detail = f"Failed to create pull request (GitHub 422): {gh_msg}"
        if err_msgs:
            detail += f" — {err_msgs}"
        logger.warning(
            f"Failed to create pull request (422 unmatched): {response.text[:500]}"
        )
        raise HTTPException(status_code=422, detail=detail[:500])

    if response.status_code == 401:
        logger.error(
            f"Failed to create pull request (401 Unauthorized): {response.text}"
        )
        raise HTTPException(
            status_code=401,
            detail="GitHub authentication failed (401 Unauthorized) — token may be expired or revoked",
        )
    logger.error(
        f"Failed to create pull request ({response.status_code}): {response.text}"
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY
        if response.status_code >= 500
        else status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Failed to create pull request (GitHub {response.status_code}): {response.text[:500]}",
    )


async def get_pull_request(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: str | None = None,
) -> dict:
    """Fetches PR details."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers,
        project_id=project_id,
    )
    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="GitHub authentication failed (401 Unauthorized) — token may be expired or revoked",
        )
    if response.status_code in (404, 410):
        raise HTTPException(
            status_code=response.status_code,
            detail=f"PR #{pr_number} not found or repo gone",
        )
    if response.status_code != 200:
        logger.error(f"Failed to get PR #{pr_number}: {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get PR #{pr_number}",
        )
    return response.json()


async def get_pull_request_reviews(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: str | None = None,
) -> list[dict]:
    """Fetch the human/bot review submissions on a PR (state APPROVED /
    CHANGES_REQUESTED / COMMENTED / DISMISSED).

    Used by the rollup PR-verify path to distinguish a genuine review rejection
    (any submission with state == CHANGES_REQUESTED) from a non-rejection
    cleanup/supersession close. Best-effort: returns ``[]`` on any non-200 so a
    transient GitHub hiccup never *masks* a rejection — the caller treats an
    empty result as "no recorded CHANGES_REQUESTED" only in combination with
    the other non-genuine signals (bot-close / supersession), and the genuine
    DB markers (pr_rejected / review_cap_exhausted) still force FAILED.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        headers,
        params={"per_page": 100},
        project_id=project_id,
    )
    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="GitHub authentication failed (401 Unauthorized) — token may be expired or revoked",
        )
    if response.status_code != 200:
        logger.warning(
            f"Failed to list reviews for PR #{pr_number} "
            f"(status={response.status_code}); treating as no recorded reviews"
        )
        return []
    data = response.json()
    return data if isinstance(data, list) else []


async def get_open_pull_requests(
    token: str, owner: str, repo: str, base: str | None = None
) -> list[dict]:
    """Fetches open PRs, optionally filtered to a specific base branch."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params: dict[str, str | int] = {"state": "open", "per_page": 100}
    if base is not None:
        params["base"] = base
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers,
        params=params,
    )
    if response.status_code == 401:
        raise HTTPException(
            status_code=401, detail="GitHub authentication failed (401 Unauthorized)"
        )
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
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers,
        timeout=30.0,
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
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits?per_page=100",
        headers,
    )
    if response.status_code == 401:
        raise HTTPException(
            status_code=401, detail="GitHub authentication failed (401 Unauthorized)"
        )
    if response.status_code != 200:
        logger.error(f"Failed to get PR commits #{pr_number}: {response.text}")
        return []
    return response.json()


async def merge_pull_request(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    merge_method: str = "squash",
    project_id: str | None = None,
) -> dict:
    """Merges a PR. Returns dict with 'merged' bool and 'message'. Does not raise on conflict."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
        headers,
        json={"merge_method": merge_method},
        project_id=project_id,
    )
    data = response.json()
    if response.status_code == 200:
        return {"merged": True, "message": data.get("message", "Merged")}
    if response.status_code in (405, 409):
        # 405 = not mergeable (checks/reviews), 409 = merge conflict
        msg = data.get("message", "Merge failed")
        logger.warning(f"PR #{pr_number} merge blocked ({response.status_code}): {msg}")
        return {
            "merged": False,
            "conflict": response.status_code == 409,
            "message": msg,
        }
    body_snippet = (response.text or "")[:500]
    logger.error(
        f"Failed to merge PR #{pr_number} (status={response.status_code}): {body_snippet}"
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Failed to merge PR #{pr_number} (upstream status {response.status_code})",
    )


async def get_pull_request_merge_state(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: str | None = None,
) -> bool | None:
    """Return the PR's authoritative ``merged`` boolean, or ``None`` if unknown.

    ``merge_pull_request`` collapses two genuinely different outcomes onto the
    same ``{"merged": False}`` result: a real merge block (failing checks,
    required reviews, a conflict) AND an *already-merged* PR (GitHub's merge
    endpoint returns 405 "Pull Request is not mergeable" once a PR has already
    been merged).  The release qualifier's ``promote()`` must not record an
    already-merged PR as a ``merge_blocked`` failure.

    This helper queries the PR *resource itself* — ``GET /repos/.../pulls/{n}``
    — and reads its top-level ``merged`` flag.  That flag is a property of the
    PR object and flips to ``true`` synchronously the moment GitHub completes a
    merge; it is NOT subject to the eventual-consistency lag that the
    ``GET /compare`` ancestry API can exhibit immediately after a merge.  So
    asking the PR directly is the robust disambiguation primitive — a PR that
    GitHub merged is reported merged here even when a compare call would still
    say ``diverged`` for a few seconds.

    Returns ``True`` / ``False`` on a definitive answer, or ``None`` when the
    PR state could not be fetched (API error, PR not found) — ``None`` is
    deliberately NOT coerced to a bool so a transient failure can never read as
    "definitely not merged" (which would misclassify a real success as a
    failure) nor as "merged" (which would skip recording a real block).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers,
        project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(
            "[GITHUB] get_pull_request_merge_state: PR #%s for %s/%s returned "
            "%s — cannot determine merge state",
            pr_number,
            owner,
            repo,
            response.status_code,
        )
        return None
    try:
        merged = response.json().get("merged")
    except (ValueError, TypeError) as exc:
        logger.warning(
            "[GITHUB] get_pull_request_merge_state: unparseable PR #%s "
            "response for %s/%s: %s",
            pr_number,
            owner,
            repo,
            exc,
        )
        return None
    # GitHub always includes a boolean ``merged`` on a single-PR response; if
    # it is somehow absent/non-bool, treat that as indeterminate rather than
    # guessing.
    if isinstance(merged, bool):
        return merged
    return None


async def enable_auto_merge(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    merge_method: str = "SQUASH",
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
    from app import github as _gh

    try:
        pr_data = await _gh.get_pull_request(
            token, owner, repo, pr_number, project_id=project_id
        )
    except HTTPException as exc:
        return {
            "enabled": False,
            "message": f"Failed to fetch PR #{pr_number}: {exc.detail}",
        }
    pr_node_id = pr_data.get("node_id")
    if not pr_node_id:
        return {
            "enabled": False,
            "message": f"PR #{pr_number} has no node_id in REST response",
        }

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
    body = {
        "query": mutation,
        "variables": {"prId": pr_node_id, "method": merge_method},
    }

    response = await _github_request(
        "POST",
        "https://api.github.com/graphql",
        headers,
        json=body,
        project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(
            f"[AUTO-MERGE] PR #{pr_number} GraphQL {response.status_code}: {response.text[:200]}"
        )
        return {"enabled": False, "message": f"GraphQL HTTP {response.status_code}"}
    data = response.json() or {}
    # GraphQL returns 200 even on application-level errors — check the errors array.
    if data.get("errors"):
        err_msgs = "; ".join(str(e.get("message", "")) for e in data["errors"])
        logger.info(f"[AUTO-MERGE] PR #{pr_number} rejected by GitHub: {err_msgs}")
        return {"enabled": False, "message": err_msgs or "GraphQL error"}
    enabled_at = (
        data.get("data", {})
        .get("enablePullRequestAutoMerge", {})
        .get("pullRequest", {})
        .get("autoMergeRequest", {})
        or {}
    ).get("enabledAt")
    if enabled_at:
        logger.info(
            f"[AUTO-MERGE] PR #{pr_number} queued for auto-merge ({merge_method}) at {enabled_at}"
        )
        return {"enabled": True, "message": f"Auto-merge enabled at {enabled_at}"}
    return {
        "enabled": False,
        "message": "GraphQL returned no enabledAt — auto-merge not confirmed",
    }


async def update_branch(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
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
        headers,
        json=body,
        project_id=project_id,
    )
    if response.status_code == 202:
        try:
            data = response.json() or {}
        except Exception:  # noqa: BLE001
            data = {}
        msg = data.get("message", "Update queued")
        logger.info(f"[UPDATE-BRANCH] PR #{pr_number} update queued: {msg}")
        return {"updated": True, "message": msg}
    if response.status_code in (403, 404, 422):
        try:
            data = response.json() or {}
        except Exception:  # noqa: BLE001
            data = {}
        msg = data.get("message", f"HTTP {response.status_code}")
        logger.info(
            f"[UPDATE-BRANCH] PR #{pr_number} cannot update ({response.status_code}): {msg}"
        )
        return {"updated": False, "message": msg}
    body_snippet = (response.text or "")[:500]
    logger.error(
        f"Failed to update PR #{pr_number} branch (status={response.status_code}): {body_snippet}"
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Failed to update PR #{pr_number} branch (upstream status {response.status_code})",
    )


# GitHub's canonical 422 reply when /update-branch is called but the PR's
# base ref has no commits the head ref doesn't already contain. This is a
# *positive* signal: the PR is not behind despite a stale cached
# mergeable_state. We tag the response with `not_behind=True` so callers
# can short-circuit just like the pr_is_not_behind=True path instead of
# treating it as a transient update-branch failure that burns a retry.
_UPDATE_BRANCH_NO_NEW_COMMITS_PHRASE = "no new commits on the base branch"


def _is_no_new_commits_message(message: str | None) -> bool:
    if not message:
        return False
    return _UPDATE_BRANCH_NO_NEW_COMMITS_PHRASE in message.strip().lower()


def _tag_not_behind_if_no_new_commits(result: dict) -> dict:
    """Annotate an update_branch result with `not_behind=True` when GitHub's
    message indicates the PR has nothing on base to incorporate."""
    if (
        not result.get("updated")
        and _is_no_new_commits_message(result.get("message"))
        and not result.get("not_behind")
    ):
        return {**result, "not_behind": True}
    return result


async def update_branch_with_sha_retry(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
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

    When GitHub responds with the canonical "no new commits on the base branch"
    message, the PR is not actually behind (cached mergeable_state is stale);
    the result is tagged with ``not_behind=True`` so callers can skip retry/fix
    escalation, mirroring the existing ``pr_is_not_behind`` short-circuit.
    """
    from app import github as _gh

    try:
        result = await _gh.update_branch(
            token,
            owner,
            repo,
            pr_number,
            expected_head_sha=expected_head_sha,
            project_id=project_id,
        )
    except HTTPException as exc:
        return {"updated": False, "message": f"HTTP error: {exc.detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"updated": False, "message": f"{type(exc).__name__}: {exc}"}
    if result.get("updated"):
        return result
    result = _tag_not_behind_if_no_new_commits(result)
    if result.get("not_behind"):
        logger.info(
            f"[UPDATE-BRANCH] PR #{pr_number} not-behind (no new commits on base) — "
            f"skipping retry, deferring to next sweep"
        )
        return result
    try:
        fresh = await _gh.get_pull_request(
            token, owner, repo, pr_number, project_id=project_id
        )
    except Exception as exc:  # noqa: BLE001
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
        retry_result = await _gh.update_branch(
            token,
            owner,
            repo,
            pr_number,
            expected_head_sha=fresh_sha,
            project_id=project_id,
        )
    except HTTPException as exc:
        return {"updated": False, "message": f"HTTP error on retry: {exc.detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"updated": False, "message": f"{type(exc).__name__} on retry: {exc}"}
    return _tag_not_behind_if_no_new_commits(retry_result)


async def close_pull_request(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: str | None = None,
) -> bool:
    """Close a PR without merging. Returns True if closed successfully."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "PATCH",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers,
        json={"state": "closed"},
        project_id=project_id,
    )
    if response.status_code == 200:
        logger.info(f"Closed PR #{pr_number} on {owner}/{repo}")
        return True
    logger.warning(
        f"Failed to close PR #{pr_number}: {response.status_code} {response.text[:200]}"
    )
    return False


async def reopen_pull_request(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: str | None = None,
) -> bool:
    """Reopen a previously closed PR. Returns True on success."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "PATCH",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
        headers,
        json={"state": "open"},
        project_id=project_id,
    )
    if response.status_code == 200:
        logger.info(f"Reopened PR #{pr_number} on {owner}/{repo}")
        return True
    logger.warning(
        f"Failed to reopen PR #{pr_number}: {response.status_code} {response.text[:200]}"
    )
    return False


async def get_pr_files(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: str | None = None,
    include_patch: bool = False,
) -> list[dict]:
    """Fetches the list of files changed in a PR with their status.

    Raises HTTPException on any non-200 response so callers can distinguish a
    genuinely empty PR (returns []) from a transient GitHub failure. The
    empty-rollup short-circuit in task_lifecycle relies on this distinction to
    avoid closing real PRs when the API is briefly unavailable.

    When include_patch=True, the returned dicts also contain a 'patch' key with
    the unified diff text for each file (may be absent for binary files).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100",
        headers,
        project_id=project_id,
    )
    if response.status_code == 401:
        raise HTTPException(
            status_code=401, detail="GitHub authentication failed (401 Unauthorized)"
        )
    if response.status_code != 200:
        logger.error(
            f"Failed to get PR files #{pr_number}: {response.status_code} {response.text[:300]}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub API error fetching PR #{pr_number} files (HTTP {response.status_code})",
        )
    result = []
    for f in response.json():
        entry: dict = {
            "filename": f["filename"],
            "status": f["status"],
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "changes": f.get("changes", 0),
        }
        if include_patch and "patch" in f:
            entry["patch"] = f["patch"]
        result.append(entry)
    return result


