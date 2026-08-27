"""Branch / tag / commit-metadata / PR-discovery / ancestry operations.

Extracted from ``app.github``; all symbols are re-exported from there so
existing imports keep working.
"""

import base64
import time
from datetime import datetime
from urllib.parse import quote

from fastapi import HTTPException, status

from app.github import logger

# Keep `datetime` referenced so linters don't strip it; it is only used as a
# forward reference in `get_commit_date(...) -> "datetime | None"`.
_DATETIME = datetime

_RECENT_COMMIT_MESSAGES_DEFAULT_LIMIT = 30
_RECENT_COMMIT_MESSAGES_MAX_LIMIT = 50
# GitHub's compare endpoint returns at most this many file entries.  It does
# not provide a total-file count, so a response at this limit is ambiguous.
_GITHUB_COMPARE_FILES_MAX = 300

# ``POST /git/refs`` answers 422 for at least THREE different conditions that
# are distinguished ONLY by the response body's ``message``:
#   * "Reference already exists" — the ref is there (benign retry, or a stale
#     branch left by a crashed run)
#   * "Object does not exist"    — the sha is unknown, invalid or ABBREVIATED
#   * "Reference update failed"  — a validation / permission refusal
# Only the first means "reuse".  Collapsing all three onto reuse is what turned
# an abbreviated ``--candidate-sha`` into a bare ``release_branch_failed`` with
# no reason at all (incident 2026-08-23).  Literal pin — do not soften.
_REF_ALREADY_EXISTS_MESSAGE = "reference already exists"
# GitHub's prose is bounded before it lands on an operator-visible record.
_GITHUB_ERROR_MESSAGE_MAX = 200


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time so
    monkeypatching ``app.github._github_request`` stays effective here."""
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


def github_error_message(response) -> str:
    """GitHub's OWN error prose for a failed response — bounded, never ``None``.

    Prefers the JSON body's ``message`` (plus any ``errors[].message`` /
    ``errors[].code``), falling back to the raw response text.  A raise built
    from a GitHub response that drops this leaves the operator a status code.
    """
    message = ""
    extras: list[str] = []
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is normal (HTML 5xx, empty)
        body = None
    if isinstance(body, dict):
        message = str(body.get("message") or "").strip()
        raw_errors = body.get("errors")
        if isinstance(raw_errors, list):
            for item in raw_errors:
                if isinstance(item, dict):
                    text = str(item.get("message") or item.get("code") or "").strip()
                else:
                    text = str(item or "").strip()
                if text and text != message:
                    extras.append(text)
    if extras:
        joined = "; ".join(extras)
        message = f"{message} ({joined})" if message else joined
    if not message:
        message = str(getattr(response, "text", "") or "").strip()
    return message[:_GITHUB_ERROR_MESSAGE_MAX]


async def create_branch(
    token: str, owner: str, repo: str, branch_name: str, sha: str
) -> dict:
    """Create a branch (git ref) at ``sha``, or report reuse of an existing one.

    Returns the GitHub ref object on a real create (HTTP 201).

    HTTP 422 is AMBIGUOUS (see ``_REF_ALREADY_EXISTS_MESSAGE``): only an explicit
    "Reference already exists" takes the reuse path below; every other 422 raises
    an error whose ``detail`` QUOTES GitHub's own message, so the reason survives
    into the operator record instead of flattening to a bare status code.

    On the reuse 422 the common cause is a retry after a 502
    whose write actually landed, but it is ALSO what a crashed prior run leaves
    behind, and that branch can be sitting at a COMPLETELY DIFFERENT SHA.  The
    old implementation fetched the live SHA and returned a success-shaped
    ``{"object": {"sha": existing_sha}}`` with NO comparison to the requested
    ``sha`` — so a caller that just checks "did this raise?" silently adopted a
    stale tip.  ``release_promotion.promote()`` already defends against this by
    hand (compare → ``delete_branch`` → recreate), which proves the hazard is
    real; the comparison belongs HERE so no caller can forget it.

    The reuse result is therefore explicitly distinguishable::

        {
          "ref": "refs/heads/<branch>",
          "object": {"sha": <existing_sha>},   # unchanged shape — old callers keep working
          "reused": True,                      # NEW: this branch was NOT created by us
          "existing_sha": <existing_sha>,      # NEW: where the pre-existing ref points
          "requested_sha": <sha>,              # NEW: where we asked it to point
          "sha_matches": <existing_sha == sha> # NEW: False ⇒ caller MUST decide
        }

    ``sha_matches: False`` is the load-bearing signal: the caller must either
    force the ref to ``sha`` (delete + recreate, as ``release_promotion`` does)
    or consciously accept the existing tip.  A real create (201) has no
    ``reused`` key, so ``result.get("reused")`` is falsy exactly when the branch
    is guaranteed to be at ``sha``.
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
        # Only take the reuse path when GitHub actually SAID the reference
        # exists.  For any other 422 (notably "Object does not exist" for an
        # unknown/abbreviated sha) the old code probed for a branch that is not
        # there, swallowed the 404 in a bare ``except Exception``, and fell
        # through to a generic error discarding ``response.text`` — the one
        # place the real reason lived.
        github_message = github_error_message(response)
        if _REF_ALREADY_EXISTS_MESSAGE not in github_message.lower():
            logger.error(
                "Failed to create branch '%s' at '%s': GitHub 422 — %s",
                branch_name,
                sha,
                github_message or "<no message from GitHub>",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Failed to create branch '{branch_name}' at '{sha}' on "
                    f"GitHub (GitHub 422): "
                    f"{github_message or '<no message from GitHub>'}"
                ),
            )
        # Branch already exists (e.g., retry after 502 that actually succeeded)
        from app import github as _gh

        try:
            existing_sha = await _gh.get_branch_sha(
                token, owner, repo, branch_name, live=True
            )
            sha_matches = bool(existing_sha) and existing_sha == sha
            if sha_matches:
                logger.info(
                    f"Branch '{branch_name}' already exists at the requested sha "
                    f"({existing_sha[:8]}) — reusing"
                )
            else:
                # NOT an error on its own — but the caller is now adopting a ref
                # that points somewhere it did not ask for. Say so loudly.
                logger.warning(
                    f"Branch '{branch_name}' already exists at a DIFFERENT sha "
                    f"(existing={existing_sha[:8] if existing_sha else '?'}, "
                    f"requested={sha[:8] if sha else '?'}) — returning reused=True "
                    f"with sha_matches=False; the caller must reset or accept it"
                )
            return {
                "ref": f"refs/heads/{branch_name}",
                "object": {"sha": existing_sha},
                "reused": True,
                "existing_sha": existing_sha,
                "requested_sha": sha,
                "sha_matches": sha_matches,
            }
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Branch '{branch_name}' 422 but get_branch_sha also failed; falling through"
            )

    if response.status_code != 201:
        github_message = github_error_message(response)
        logger.error(f"Failed to create branch: {response.status_code} {response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Failed to create branch '{branch_name}' at '{sha}' on GitHub "
                f"(GitHub {response.status_code})"
                + (f": {github_message}" if github_message else "")
                + "."
            ),
        )

    return response.json()


async def merge_branch(
    token: str,
    owner: str,
    repo: str,
    base: str,
    head: str,
    *,
    commit_message: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Merge ``head`` into ``base`` via the GitHub merges API (server-side).

    Wraps ``POST /repos/{owner}/{repo}/merges``. This is the safe "rebase the
    branch from another branch" primitive: it brings every commit on ``head``
    into ``base`` with a merge commit, WITHOUT touching commits already on
    ``base`` (so sibling rollup work on a shared staging branch is preserved).

    Used to re-cut a held cross-rollup code-dependent's staging/rollup base from
    current integration once its dependency has landed there, so the dependent's
    base provably contains the landed code without re-homing it onto integration.

    Returns a small status dict so callers never have to keyword-match GitHub
    prose:

      * ``{"merged": True,  "already": False}`` — a merge commit was created
        (HTTP 201).
      * ``{"merged": True,  "already": True}``  — ``base`` already contains
        ``head``; nothing to do (HTTP 204).
      * ``{"merged": False, "conflict": True}`` — the merge would conflict
        (HTTP 409); the caller must NOT dispatch against this base.
      * ``{"merged": False, "missing": True}``  — ``base`` or ``head`` does not
        exist (HTTP 404).

    Never raises for an API error — callers degrade on the returned flags.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: dict = {"base": base, "head": head}
    if commit_message:
        payload["commit_message"] = commit_message

    response = await _github_request(
        "POST",
        f"https://api.github.com/repos/{owner}/{repo}/merges",
        headers,
        json=payload,
        project_id=project_id,
    )
    status_code = response.status_code
    if status_code == 201:
        logger.info(
            "[GITHUB] merge_branch: merged %s into %s for %s/%s",
            head,
            base,
            owner,
            repo,
        )
        return {"merged": True, "already": False, "status": status_code}
    if status_code == 204:
        # Nothing to merge — base already contains head.
        return {"merged": True, "already": True, "status": status_code}
    if status_code == 409:
        logger.warning(
            "[GITHUB] merge_branch: conflict merging %s into %s for %s/%s",
            head,
            base,
            owner,
            repo,
        )
        return {"merged": False, "conflict": True, "status": status_code}
    if status_code == 404:
        logger.warning(
            "[GITHUB] merge_branch: base %r or head %r not found for %s/%s",
            base,
            head,
            owner,
            repo,
        )
        return {"merged": False, "missing": True, "status": status_code}
    logger.error(
        "[GITHUB] merge_branch: unexpected %s merging %s into %s for %s/%s: %s",
        status_code,
        head,
        base,
        owner,
        repo,
        response.text[:300],
    )
    return {"merged": False, "status": status_code}


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


async def get_branch_sha(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
    *,
    live: bool = False,
) -> str:
    """Gets the latest commit SHA of a branch.

    Cache-by-default: the result is served from / populated into the short-TTL
    branch-tip cache shared with the repo-snapshot layer
    (``github_repo_snapshot._branch_sha_cache``, ~60s). This is safe for the
    overwhelming majority of reads — behind-detection ("is this PR behind its
    base?"), grounding/provenance, observability sweeps — where a ≤60s-old tip
    is fine (GitHub still enforces mergeability at merge time), and it removes
    the repeated identical ``git/ref/heads/{base}`` reads (every concurrent
    PR/sweep checks the same base tip) that dominate GitHub-quota burn.

    Pass ``live=True`` for SHA-CRITICAL reads that MUST observe the current tip:
    recording a promoted/merged SHA, pinning the canonical candidate SHA, or
    anchoring a branch create/reset. ``live=True`` skips the cache on BOTH read
    and write-back, so an anchor read neither trusts nor pollutes the shared tip
    cache. The read-only allowlist test ``TestGitHubRefReadCaching`` enforces
    that every ``live=True`` site is a reviewed member of the must-be-live set.

    A cache MISS still does the live request and raises on failure, preserving
    the str-or-raise contract.
    """
    if not live:
        from app.github_repo_snapshot import (
            _BRANCH_SHA_TTL_S,
            _branch_sha_cache,
            _bump_cache_stat,
            _get_shared_branch_sha_response,
        )

        _hit = _branch_sha_cache.get((owner, repo, branch))
        if _hit is not None and (time.monotonic() - _hit[1]) < _BRANCH_SHA_TTL_S:
            _bump_cache_stat("branch_sha_snapshot")
            return _hit[0]

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if live:
        response = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
            headers,
            project_id=project_id,
        )
    else:
        response = await _get_shared_branch_sha_response(
            (owner, repo, branch),
            lambda: _github_request(
                "GET",
                f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
                headers,
                project_id=project_id,
            ),
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
    sha = response.json()["object"]["sha"]
    if not live:
        from app.github_repo_snapshot import _branch_sha_cache

        _branch_sha_cache[(owner, repo, branch)] = (sha, time.monotonic())
    return sha


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


async def repo_has_commits(token: str, owner: str, repo: str) -> bool:
    """True when the repository has at least one branch (i.e. at least one commit).

    A GitHub repository created without an initial commit still *reports* a
    ``default_branch`` (usually ``main``) from ``GET /repos/{owner}/{repo}`` —
    but that ref does not exist. The repo has no branches and no refs at all.
    ``get_repo_default_branch`` therefore cannot distinguish a real trunk from a
    phantom one; this can.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/branches",
        headers,
        params={"per_page": 1},
    )
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"GitHub API error listing branches for {owner}/{repo} "
                f"(HTTP {response.status_code})."
            ),
        )
    branches = response.json()
    return isinstance(branches, list) and len(branches) > 0


async def ensure_repo_initialized(
    token: str,
    owner: str,
    repo: str,
    default_branch: str,
    readme_body: str | None = None,
) -> bool:
    """Give a commitless repository its first commit, creating ``default_branch``.

    A repo with zero commits has no refs, so every clone in the fleet — all of
    which pass ``--branch <trunk>`` — dies with ``fatal: Remote branch <trunk>
    not found in upstream origin`` (exit 128). That strands ``repo_bootstrap``,
    the very task meant to seed the project, in a retry loop it cannot escape.

    The Git Data API cannot be used to fix this: ``POST /git/trees``,
    ``/git/commits`` and ``/git/refs`` all reject a commitless repository with
    409 ``"Git Repository is empty"``. The Contents API is the only endpoint
    that can create the first commit, and it requires a file — hence the README
    seed rather than an empty-tree commit.

    Idempotent: returns False without writing when the repo already has a
    commit. Returns True when this call created the initial commit.
    """
    if await repo_has_commits(token, owner, repo):
        return False

    body = readme_body if readme_body else f"# {repo}\n"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "message": "Initialize repository",
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "branch": default_branch,
    }
    response = await _github_request(
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/contents/README.md",
        headers,
        json=payload,
    )

    # 409/422 here means someone else seeded the repo between our check and this
    # write. Confirm a commit actually exists now rather than assuming it does —
    # a wrong "already initialized" would hand the caller the same phantom trunk
    # this function exists to prevent.
    if response.status_code in (409, 422) and await repo_has_commits(
        token, owner, repo
    ):
        logger.info(
            f"[SEED] {owner}/{repo} was initialized concurrently; nothing to do"
        )
        return False

    if response.status_code not in (200, 201):
        logger.error(
            f"[SEED] Failed to initialize empty repo {owner}/{repo}: "
            f"{response.status_code} {response.text}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Could not create the initial commit on empty repository "
                f"{owner}/{repo} (GitHub {response.status_code}). The GitHub App "
                f"installation needs contents:write to seed an empty repo."
            ),
        )

    logger.info(
        f"[SEED] Initialized empty repo {owner}/{repo}: first commit on '{default_branch}'"
    )
    return True


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


async def get_git_commit_identity(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    *,
    project_id: str | None = None,
) -> dict | None:
    """Return an exact commit's immutable tree and ordered parent identities.

    This intentionally accepts only a canonical full commit SHA and verifies the
    response repeats that exact SHA.  Callers use it as positive provenance, so
    an API error or malformed response returns ``None`` rather than guessing.
    """
    from app.github_commit_resolution import is_full_commit_sha

    if not is_full_commit_sha(sha):
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = await _github_request(
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/git/commits/{sha}",
            headers,
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 -- provenance lookup fails closed
        logger.warning(
            "[GITHUB] get_git_commit_identity failed for %s/%s@%s: %s",
            owner,
            repo,
            sha[:8],
            exc,
        )
        return None
    if response.status_code != 200:
        logger.warning(
            "[GITHUB] get_git_commit_identity: %s/%s sha=%s returned %s",
            owner,
            repo,
            sha[:8],
            response.status_code,
        )
        return None
    try:
        body = response.json()
        if not isinstance(body, dict):
            return None
        resolved_sha = body.get("sha")
        tree = body.get("tree")
        raw_parents = body.get("parents")
        if (
            not isinstance(resolved_sha, str)
            or not isinstance(tree, dict)
            or not isinstance(tree.get("sha"), str)
            or not isinstance(raw_parents, list)
        ):
            return None
        tree_sha = tree["sha"]
        parents = []
        for parent in raw_parents:
            if not isinstance(parent, dict) or not isinstance(parent.get("sha"), str):
                return None
            parents.append(parent["sha"])
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        resolved_sha != sha
        or not is_full_commit_sha(tree_sha)
        or any(not is_full_commit_sha(parent) for parent in parents)
    ):
        return None
    return {"sha": resolved_sha, "tree_sha": tree_sha, "parents": parents}


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
            k for k, (_, ts) in _list_prs_for_commit_cache.items() if ts < cutoff
        ]:
            _list_prs_for_commit_cache.pop(k, None)
    return all_prs


# Paths whose changes trigger the agent VM image bake workflow
# (.github/workflows/packer-agent.yml ``on.push.paths``). Keep in sync with that
# workflow: a promoted commit that touches any of these expects a fresh Packer
# bake of the agent VM image.
AGENT_IMAGE_BAKE_PATHS = ("agent/", "infra/packer/")


async def agent_image_rebake_expected(
    token: str,
    owner: str,
    repo: str,
    base_ref: str,
    head_ref: str,
    project_id: str | None = None,
) -> bool | None:
    """True iff the diff ``base_ref...head_ref`` touches a bake-triggering path.

    The agent VM image bake workflow runs only when a push to the production
    branch changes ``agent/**`` or ``infra/packer/**`` (see
    ``.github/workflows/packer-agent.yml``).  So a promoted candidate "expects a
    rebake" exactly when its diff against the previously-deployed SHA touches one
    of those paths.  Reuses the existing compare helper
    :func:`get_files_changed_between_refs` rather than re-deriving the diff.

    This is a serving-gate input: a promotion whose code changed the agent image
    but whose bake never ran (or failed) is only partially served, even though
    the backend revision deployed cleanly.

    Returns ``True``/``False`` definitively, or ``None`` when the comparison
    could not be made (API error / ref not found) so the caller can treat
    indeterminate as such rather than silently assuming no rebake is needed.
    """
    files = await get_files_changed_between_refs(
        token, owner, repo, base_ref, head_ref, project_id=project_id
    )
    if files is None:
        return None
    return any(f.startswith(prefix) for f in files for prefix in AGENT_IMAGE_BAKE_PATHS)


async def compare_ref_status(
    token: str,
    owner: str,
    repo: str,
    base_ref: str,
    head_ref: str,
    project_id: str | None = None,
) -> str | None:
    """Return GitHub's RAW compare status for ``base_ref...head_ref``.

    One of the four values GitHub's compare API reports, from the perspective of
    ``head_ref`` relative to ``base_ref``:

      * ``"identical"`` — same commit.
      * ``"ahead"``     — ``head_ref`` strictly contains ``base_ref`` plus more.
      * ``"behind"``    — ``head_ref`` is an ANCESTOR of ``base_ref``: it has NO
        commits of its own that ``base_ref`` lacks. Fast-forwarding ``head_ref``
        to ``base_ref`` therefore destroys nothing.
      * ``"diverged"``  — BOTH refs have commits the other lacks. ``head_ref``
        carries unique work; resetting it DESTROYS that work.

    ``None`` when the comparison could not be made (API error / ref not found).

    Why this exists separately from :func:`is_ancestor_of`: that helper collapses
    the four statuses onto a bool, and ``"behind"`` and ``"diverged"`` BOTH map to
    ``False``.  For a read-only "has this landed?" question that collapse is
    correct.  For a DESTRUCTIVE decision ("may I delete and recreate this ref?")
    it is catastrophic — the two statuses have opposite answers, and only the raw
    status can tell them apart.  Any caller about to delete a ref MUST use this.
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
            "[GITHUB] compare_ref_status: compare %s...%s for %s/%s returned %s — "
            "cannot determine ancestry",
            base_ref[:8] if base_ref else base_ref,
            head_ref,
            owner,
            repo,
            response.status_code,
        )
        return None
    try:
        raw = response.json().get("status")
    except (ValueError, TypeError) as exc:
        logger.warning(
            "[GITHUB] compare_ref_status: unparseable compare response for %s/%s: %s",
            owner,
            repo,
            exc,
        )
        return None
    return raw if isinstance(raw, str) else None


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

    NOTE the deliberate lossiness: ``"behind"`` and ``"diverged"`` BOTH return
    ``False``.  That is correct for the reachability question this answers, but it
    means a ``False`` here does NOT license destroying ``descendant_ref`` — a
    diverged ref holds unique commits.  Destructive callers must use
    :func:`compare_ref_status` and inspect the raw status.
    """
    compare_status = await compare_ref_status(
        token, owner, repo, ancestor_sha, descendant_ref, project_id=project_id
    )
    if compare_status is None:
        return None
    # "identical" (same commit) or "ahead" (descendant strictly contains the
    # ancestor) both mean ancestor_sha is reachable from descendant_ref.
    return compare_status in ("identical", "ahead")


async def list_recent_commit_messages(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    limit: int = _RECENT_COMMIT_MESSAGES_DEFAULT_LIMIT,
    project_id: str | None = None,
) -> list[str] | None:
    """Return recent commit messages from ``branch`` using one bounded API call."""
    try:
        per_page = max(1, min(int(limit), _RECENT_COMMIT_MESSAGES_MAX_LIMIT))
    except (TypeError, ValueError):
        per_page = _RECENT_COMMIT_MESSAGES_DEFAULT_LIMIT

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    encoded_branch = quote(branch, safe="")
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/commits"
        f"?sha={encoded_branch}&per_page={per_page}",
        headers,
        project_id=project_id,
    )
    if response.status_code != 200:
        logger.warning(
            "[GITHUB] list_recent_commit_messages: %s/%s@%s returned %s",
            owner,
            repo,
            branch,
            response.status_code,
        )
        return None
    try:
        commits = response.json()
    except (ValueError, TypeError) as exc:
        logger.warning(
            "[GITHUB] list_recent_commit_messages: unparseable response for "
            "%s/%s@%s: %s",
            owner,
            repo,
            branch,
            exc,
        )
        return None
    if not isinstance(commits, list):
        logger.warning(
            "[GITHUB] list_recent_commit_messages: expected list for %s/%s@%s, got %s",
            owner,
            repo,
            branch,
            type(commits).__name__,
        )
        return None

    messages: list[str] = []
    for entry in commits:
        commit = entry.get("commit") if isinstance(entry, dict) else None
        message = commit.get("message") if isinstance(commit, dict) else None
        if isinstance(message, str):
            messages.append(message)
    return messages


async def check_squash_referenced(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    pr_number: int,
    commit_shas: list[str] | None = None,
    project_id: str | None = None,
) -> bool | None:
    """Return True if recent commits on ``branch`` reference ``pr_number`` or any
    of ``commit_shas``, False if no reference was found, or None on API error.

    Used as a fallback after ``is_ancestor_of`` returns False to detect squash-
    merged PRs: the pre-squash ``merge_commit_sha`` stored on the PR object is
    not an ancestor of the integration branch after a squash merge, but the
    squash commit message typically contains ``#<pr_number>`` or includes the
    original commit SHAs in its body.
    """
    messages = await list_recent_commit_messages(
        token, owner, repo, branch, limit=50, project_id=project_id
    )
    if messages is None:
        return None
    pr_ref = f"#{pr_number}"
    short_shas = {sha[:8] for sha in (commit_shas or []) if sha and len(sha) >= 8}
    for msg in messages:
        if pr_ref in msg:
            return True
        for short in short_shas:
            if short in msg:
                return True
    return False


async def get_changed_files_with_patches_between_refs(
    token: str,
    owner: str,
    repo: str,
    base_ref: str,
    head_ref: str,
    project_id: str | None = None,
) -> list[dict] | None:
    """Return ``[{"filename": ..., "patch": ...}, ...]`` for ``base...head``.

    Like :func:`get_files_changed_between_refs`, but also carries GitHub's
    per-file unified-diff ``patch`` (``None`` for binary/oversized files GitHub
    omits).  Used by the hotfix-deploy scope check, which needs BOTH the changed
    filenames (to enforce the allowlist) and the ``backend/app/main.py`` patch
    (to detect an ``EXPECTED_SCHEMA_REVISION`` bump) from a single compare call.

    Returns ``None`` on API error so callers can fail closed on an indeterminate
    diff rather than treating it as an empty change set.
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
            "[GITHUB] get_changed_files_with_patches_between_refs: compare %s...%s "
            "for %s/%s returned %s — cannot determine changed files",
            base_ref,
            head_ref,
            owner,
            repo,
            response.status_code,
        )
        return None
    try:
        payload = response.json()
        files = payload["files"]
        # GitHub caps the inline compare ``files`` collection.  Authorization
        # callers must never mistake that prefix for the complete diff: a
        # non-allowlisted path outside it could otherwise pass the hotfix gate.
        # GitHub documents this collection as capped at 300 entries and does
        # not return a total-file count.  A response at the cap could omit
        # non-allowlisted files, so it is indeterminate for authorization.
        parsed_files = [
            {"filename": f["filename"], "patch": f.get("patch")}
            for f in files
            if isinstance(f, dict) and isinstance(f.get("filename"), str) and f["filename"]
        ]
        if (
            not isinstance(files, list)
            or len(files) >= _GITHUB_COMPARE_FILES_MAX
            or len(parsed_files) != len(files)
        ):
            logger.warning(
                "[GITHUB] get_changed_files_with_patches_between_refs: incomplete "
                "compare response for %s/%s (returned=%s) — "
                "cannot authorize a partial diff",
                owner,
                repo,
                len(parsed_files) if isinstance(files, list) else "non-list",
            )
            return None
        return parsed_files

    except (ValueError, TypeError, KeyError) as exc:
        logger.warning(
            "[GITHUB] get_changed_files_with_patches_between_refs: unparseable "
            "compare response for %s/%s: %s",
            owner,
            repo,
            exc,
        )
        return None


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
