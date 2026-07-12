"""Pull-request lifecycle mutations plus read-only PR-state / ancestry checks.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

import time
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.github import logger


# Default backoff (seconds) advertised to the agent when we must refuse a create
# because the pre-create idempotency lookup could not be confirmed but GitHub
# gave us no usable reset hint. Small enough to retry promptly once a transient
# blip clears; large enough not to hot-loop.
_CREATE_PR_DEFAULT_RETRY_AFTER_S = 30

# Upper bound on the Retry-After we advertise. A drained 5000/hr install-token
# bucket can be up to ~1h from reset; telling the agent to sleep that long inside
# a synchronous ``./pr`` step would starve the reaper heartbeat budget on the VM
# and get it false-reaped. Cap it and let the agent re-poll after the window.
_CREATE_PR_MAX_RETRY_AFTER_S = 120


@dataclass
class _OpenPRLookup:
    """Outcome of the pre-create idempotency lookup for a (head, base) pair.

    ``outcome`` is the load-bearing field — the three cases demand different
    handling in :func:`create_pull_request`:

    * ``"found"``     — an open PR already exists; ``pr`` holds it. Dedup:
                        resume it instead of creating a duplicate.
    * ``"none"``      — GitHub returned HTTP 200 with an empty list. A
                        *confirmed* "no open PR", so creation is safe.
    * ``"uncertain"`` — the lookup could not be confirmed: the GitHub GET was
                        rate-limited, short-circuited by the local limiter,
                        returned a non-200, or raised. We do NOT know whether an
                        open PR exists, so POSTing now risks creating a duplicate
                        once the quota window resets (incident #7350/#7351 class).
                        The caller must refuse and surface a retryable signal.
                        ``retry_after`` carries the best-known backoff (seconds)
                        when derivable from the response.
    """

    outcome: str
    pr: dict | None = None
    retry_after: int | None = None


def _retry_after_seconds_from_response(resp) -> int | None:
    """Best-effort backoff (seconds) from a rate-limited GitHub response.

    Prefers an explicit ``Retry-After`` header (integer seconds — what GitHub
    sends for secondary limits, and what the local short-circuit synthesises from
    ``X-RateLimit-Reset``). Falls back to ``X-RateLimit-Reset`` (epoch seconds)
    minus now. Returns ``None`` when neither yields a positive value.
    """
    headers = getattr(resp, "headers", None) or {}
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            secs = int(str(retry_after).strip())
            if secs > 0:
                return secs
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            delta = int(str(reset).strip()) - int(time.time())
            if delta > 0:
                return delta
        except (TypeError, ValueError):
            pass
    return None


def _raise_create_pr_retryable(
    head: str, base: str, retry_after: int | None
) -> None:
    """Refuse a PR create with a retryable signal instead of failing open.

    Raises HTTP 503 with a ``Retry-After`` header so the agent backs off and
    retries later — replacing the old fail-open behavior that POSTed anyway when
    the idempotency lookup was inconclusive and created a duplicate PR once the
    drained GitHub quota reset. 503 (not 500) is what keeps the agent's backoff
    loop engaged rather than treating the outcome as a hard failure.
    """
    delay = retry_after or _CREATE_PR_DEFAULT_RETRY_AFTER_S
    delay = max(1, min(delay, _CREATE_PR_MAX_RETRY_AFTER_S))
    logger.warning(
        f"[PR] Refusing to create PR for {head} -> {base}: could not confirm "
        f"whether an open PR already exists (GitHub rate-limited/unavailable). "
        f"Returning 503 retry-after={delay}s to avoid a duplicate PR."
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Cannot verify whether an open pull request already exists for "
            f"{head} -> {base} because the GitHub API is rate-limited; refusing "
            "to create to avoid a duplicate PR. Retry after the window resets."
        ),
        headers={"Retry-After": str(delay)},
    )


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
            # Behind-detection base tip — staleness-tolerant, so serve from the
            # shared ~60s branch-tip cache. Many concurrent PRs check the same
            # base (main/automain) every poll; this collapses those repeated
            # git/ref reads to one per base per TTL window.
            base_tip_sha = await _gh.get_branch_sha(
                token,
                owner,
                repo,
                base_branch,
                project_id=project_id,
            )  # behind-detection base tip — cached-by-default (staleness-tolerant)
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
) -> _OpenPRLookup:
    """Look up the open PR for ``head`` -> ``base`` for create-time idempotency.

    GitHub allows at most one open PR per (head, base) pair but enforces it only
    best-effort — two concurrent ``POST /pulls`` can both succeed (201) and leave
    a duplicate (rollup PR incident #7350/#7351). This lookup lets
    ``create_pull_request`` collapse the "already created" case into resuming the
    existing PR.

    Returns an :class:`_OpenPRLookup` whose ``outcome`` distinguishes the three
    cases the caller must treat differently — crucially, a *confirmed* "no PR"
    (HTTP 200 with an empty list) from an *unconfirmed* lookup (rate-limited /
    short-circuited / non-200 / error). This is a FAIL-CLOSED contract: an
    unconfirmed lookup must NOT be read as "no PR exists", because POSTing then
    can create a duplicate PR once the drained GitHub quota resets (the old
    fail-open behavior returned ``None`` here and POSTed anyway).
    """
    try:
        list_resp = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls"
            f"?head={owner}:{head}&base={base}&state=open",
            headers,
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 — any lookup failure (network, auth, HTTP) is UNCERTAIN, not "no PR". Fail closed so we never POST a duplicate on a flaky pre-check.
        logger.warning(f"[PR] Pre-create lookup for {head} -> {base} failed: {exc}")
        return _OpenPRLookup("uncertain")

    status_code = getattr(list_resp, "status_code", None)
    if status_code == 200:
        try:
            items = list_resp.json()
        except Exception:  # noqa: BLE001 — a malformed 200 body cannot confirm "no PR"; treat as uncertain rather than fail open.
            logger.warning(
                f"[PR] Pre-create lookup for {head} -> {base} returned an "
                f"unparseable 200 body — treating as uncertain"
            )
            return _OpenPRLookup("uncertain")
        if items:
            return _OpenPRLookup("found", pr=items[0])
        return _OpenPRLookup("none")

    # Non-200: rate-limited (real GitHub 403/429 or the local short-circuit's
    # synthetic 403) or otherwise unavailable. We cannot confirm the absence of
    # an open PR, so the caller must NOT proceed to POST.
    retry_after = _retry_after_seconds_from_response(list_resp)
    logger.warning(
        f"[PR] Pre-create lookup for {head} -> {base} returned HTTP "
        f"{status_code} (rate-limited/unavailable) — treating as uncertain"
        + (f"; retry-after {retry_after}s" if retry_after else "")
    )
    return _OpenPRLookup("uncertain", retry_after=retry_after)


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
    #
    # FAIL-CLOSED: if the lookup could not be *confirmed* (rate-limited /
    # short-circuited / non-200 / error) we must NOT POST — doing so would create
    # a duplicate PR once the drained GitHub quota resets. Surface a retryable
    # 503 + Retry-After so the agent backs off and re-checks later instead.
    lookup = await _find_open_pull_request(headers, owner, repo, head, base, project_id)
    if lookup.outcome == "found" and lookup.pr is not None:
        logger.info(
            f"[PR] Open PR #{lookup.pr.get('number')} already exists for "
            f"{head} -> {base} — returning it instead of creating a duplicate"
        )
        return lookup.pr
    if lookup.outcome == "uncertain":
        _raise_create_pr_retryable(head, base, lookup.retry_after)
    # lookup.outcome == "none": GitHub confirmed no open PR — safe to create.

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
                lookup = await _find_open_pull_request(
                    headers, owner, repo, head, base, project_id
                )
                if lookup.pr is not None:
                    return lookup.pr
                # GitHub says a PR exists but we could not fetch it (the lookup
                # was itself rate-limited). Surface retryable rather than a 422
                # the agent would treat as permanent — the existing PR resolves
                # on retry once quota returns.
                if lookup.outcome == "uncertain":
                    _raise_create_pr_retryable(head, base, lookup.retry_after)
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

    # Rate-limited POST (real GitHub 403/429 or the local short-circuit's
    # synthetic 403): the create did NOT happen. Surface as a retryable 503 so
    # the agent backs off — instead of a 500 it would treat as a hard failure and
    # hot-retry, re-draining the quota and re-tripping the limiter.
    if response.status_code == 429 or (
        response.status_code == 403
        and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or response.headers.get("Retry-After") is not None
        )
    ):
        logger.warning(
            f"[PR] create POST for {head} -> {base} rate-limited "
            f"({response.status_code}) — returning retryable 503"
        )
        _raise_create_pr_retryable(
            head, base, _retry_after_seconds_from_response(response)
        )

    logger.error(
        f"Failed to create pull request ({response.status_code}): {response.text}"
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY
        if response.status_code >= 500
        else status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to create pull request",
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
    """Fetches open PRs, optionally filtered to a specific base branch.

    Follows GitHub's Link pagination so repos with more than 100 open PRs
    are fully enumerated.  Bounded at 30 pages (3 000 PRs) to match the
    hard cap used by other list helpers in this module.

    On a transient GitHub error mid-pagination, returns the already-fetched
    pages instead of discarding them.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_params: dict[str, str | int] = {"state": "open", "per_page": 100}
    if base is not None:
        base_params["base"] = base
    result: list[dict] = []
    for page in range(1, 31):
        response = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers,
            params={**base_params, "page": page},
        )
        if response.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="GitHub authentication failed (401 Unauthorized)",
            )
        if response.status_code != 200:
            logger.error(f"Failed to list PRs (base={base}): {response.text}")
            return result
        batch = response.json()
        result.extend(batch)
        if len(batch) < 100:
            break
    return result


async def get_pull_requests_for_head_branch(
    token: str,
    owner: str,
    repo: str,
    head_branch: str,
    base: str | None = None,
) -> list[dict]:
    """Fetch closed PRs whose HEAD is ``head_branch``.

    Used by the EVALUATING-timeout recovery sweep to detect a merged rollup PR
    when the ``rollup_pr_number`` context marker was lost in a crash window.

    Queries GitHub with ``state=closed`` and ``head={owner}:{head_branch}`` so
    only PRs originating from that branch are returned.  The caller must check
    ``pr.get("merged")`` (or ``pr.get("merged_at") is not None``) to distinguish
    merged from force-closed PRs.

    Returns ``[]`` on any non-200 response — callers must treat errors as
    "not found" and fall back to current behavior.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params: dict[str, str | int] = {
        "state": "closed",
        "head": f"{owner}:{head_branch}",
        "per_page": 10,
    }
    if base is not None:
        params["base"] = base
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers,
        params=params,
    )
    if response.status_code != 200:
        logger.warning(
            f"Failed to list closed PRs for head {head_branch!r}: {response.status_code}"
        )
        return []
    data = response.json()
    return data if isinstance(data, list) else []


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

    Pages through the files endpoint (100 per page, GitHub's max) until a short
    page so a PR with more than 100 changed files is not silently truncated to
    its first page — migration-safety detection must see EVERY changed file, not
    just the first 100 (a large release rollup routinely exceeds that, and a
    missed migration file on a later page would read as "no migrations" and skip
    the chain guard). Bounded at GitHub's 30-page / 3000-file hard cap.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    result = []
    for page in range(1, 31):
        response = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
            f"?per_page=100&page={page}",
            headers,
            project_id=project_id,
        )
        if response.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="GitHub authentication failed (401 Unauthorized)",
            )
        if response.status_code != 200:
            logger.error(
                f"Failed to get PR files #{pr_number} (page {page}): "
                f"{response.status_code} {response.text[:300]}"
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"GitHub API error fetching PR #{pr_number} files (HTTP {response.status_code})",
            )
        batch = response.json()
        for f in batch:
            entry: dict = {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
            }
            if "previous_filename" in f:
                entry["previous_filename"] = f["previous_filename"]
            if include_patch and "patch" in f:
                entry["patch"] = f["patch"]
            result.append(entry)
        # A short page (fewer than the per_page max) is the last page. An empty
        # first page is a genuinely empty PR and returns [] (status non-200 on
        # the first page already raised above, preserving the transient-vs-empty
        # distinction callers rely on).
        if len(batch) < 100:
            break
    return result
