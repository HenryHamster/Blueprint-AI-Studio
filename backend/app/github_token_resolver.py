"""GitHub per-task token resolution and in-process token cache.

Extracted from ``app.routers.agent`` (which had previously inherited it from
``app.main``) so that the 15+ non-router modules that resolve a GitHub token
no longer transitively depend on the entire agent FastAPI router. The router
re-exports these symbols unchanged, so every existing
``from app.routers.agent import get_github_token_for_task`` (and friends)
keeps working.

This module is intentionally router-free.

TOKEN FRESHNESS INVARIANT
-------------------------
Every token handed to a caller must still have at least
``_TOKEN_MIN_REMAINING_LIFE`` seconds of life left, and no cache here may
serve a token past its true expiry.

The subtlety this module got wrong for a long time: a cache TTL is only
sound if it is anchored at the token's MINT time. The old cache stored
``fetched_at`` — the moment the RESOLVER inserted the entry — and expired
entries 50 min after that. But the token it inserted came from
``github_auth.get_repo_installation_token``, whose own (correct) cache serves
a minted token for up to 50 min. So a token could be inserted here when it was
already 49 min old and then served for another 50 min — i.e. ~40 min past its
60-min expiry. The reactive 401 -> invalidate -> retry path was masking this.

The fix is structural, not a tuning change:

  * Tokens obtained from ``github_auth.get_repo_installation_token`` are NOT
    stored here at all. That module already owns a mint-anchored,
    single-flighted, per-repo cache and is the single authority for them.
    Double-caching them behind a second, later-anchored TTL is precisely what
    created the stale window. Falling through to it costs a dict lookup and
    ZERO GitHub calls while it is warm, so mint volume is unchanged.
  * Tokens this module MINTS ITSELF (the stored-installation fallback and the
    Secret-Manager PAT fallback) have no other cache — they are the reason
    this cache still exists. For those, mint time == insert time, so an
    absolute ``expires_at`` recorded at insert is correct by construction.

Reads are margin-checked against ``expires_at``, so an entry is dropped rather
than served once it gets close to expiry.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Tuple

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.shared_state as _shared_state
from app.field_encryption import decrypt_field
from app.github import generate_installation_access_token
from app.models import Project, Task, UserIntegration, _utcnow
from app.secrets_manager import get_user_secret

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub token cache (moved from main.py -> routers/agent.py -> here)
# ---------------------------------------------------------------------------

# project_id -> {"token", "owner", "repo", "fetched_at", "expires_at"}
#
# INVARIANT: only tokens minted BY THIS MODULE are stored here, so ``expires_at``
# is always anchored at mint time. Never insert a token obtained from
# github_auth's per-repo cache — see the module docstring.
_github_token_cache: Dict[str, Dict] = {}

# Guards mutation of the dicts in this module, and nothing more. It is held for
# dict operations only, NEVER across network I/O — so on its own it does not
# prevent duplicate mints. (It used to claim it did; it never has.) Single-flight
# is provided by:
#   * github_auth's own lock, for App-discovery tokens, and
#   * the per-project mint locks below, for tokens THIS module mints.
_github_token_cache_lock = asyncio.Lock()

# Per-project single-flight locks for the SELF-MINTED fallback path
# (generate_installation_access_token / Secret-Manager PAT). Those mints happen
# outside _github_token_cache_lock, so without this N concurrent callers on one
# cold project would fire N mints at the shared 5,000/hr installation-token
# bucket.
#
# Deliberately PER-PROJECT rather than reusing the global cache lock: the mint
# does network I/O (GitHub + Secret Manager), and holding one process-wide lock
# across that I/O would head-of-line block every other project's token
# resolution behind the slowest mint.
_project_mint_locks: Dict[str, asyncio.Lock] = {}


def _get_project_mint_lock(project_id: str) -> asyncio.Lock:
    """Return (creating on first use) the single-flight mint lock for a project.

    Safe without its own guard: asyncio is single-threaded and there is no await
    between the lookup and the insert.
    """
    lock = _project_mint_locks.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _project_mint_locks[project_id] = lock
    return lock


# True lifetime of a GitHub installation access token, per GitHub's docs.
_INSTALLATION_TOKEN_LIFETIME = 3600  # 60 minutes

# A token is only served if it still has at least this much life left. Combined
# with the 60-min lifetime above this reproduces the historical 50-min effective
# TTL exactly, so the mint rate is unchanged — but it is now measured against the
# token's TRUE expiry rather than against when we happened to cache it.
_TOKEN_MIN_REMAINING_LIFE = 600  # 10 minutes

# Retained for backwards compatibility: the effective serve-window for a
# freshly minted token (60 min lifetime - 10 min margin = 50 min). Some call
# sites and tests still import this.
_GITHUB_TOKEN_TTL = _INSTALLATION_TOKEN_LIFETIME - _TOKEN_MIN_REMAINING_LIFE  # 3000

# project_id -> (owner, repo). The cheap resolution, kept even when the token
# itself is not cached, so that the synchronous, session-less
# invalidate_github_token_cache(project_id) can find which repo's mint-anchored
# cache in github_auth must be dropped.
_project_repo_index: Dict[str, Tuple[str, str]] = {}


def _remaining_life(entry: Dict) -> float:
    """Seconds of life left on a cached entry (negative once expired)."""
    expires_at: datetime | None = entry.get("expires_at")
    if expires_at is None:
        # Entry predates expiry tracking (or was hand-injected) — treat as dead
        # rather than guessing. Fail closed: a re-mint is cheap, a dead token is not.
        return -1.0
    return (expires_at - _utcnow()).total_seconds()


def get_cached_project_token(project_id: str) -> Dict | None:
    """Return a cached entry only if its token still has a comfortable margin of life.

    Expired / nearly-expired entries are evicted rather than returned, so no
    caller can ever be handed a token past its true expiry. Callers must hold
    ``_github_token_cache_lock``.
    """
    entry = _github_token_cache.get(project_id)
    if not entry:
        return None
    if _remaining_life(entry) < _TOKEN_MIN_REMAINING_LIFE:
        _github_token_cache.pop(project_id, None)
        return None
    return entry


def cache_project_token(
    project_id: str,
    token: str,
    owner: str,
    repo: str,
    *,
    lifetime_seconds: int = _INSTALLATION_TOKEN_LIFETIME,
) -> None:
    """Cache a token THIS PROCESS JUST MINTED, anchoring expiry at mint time.

    Only call this immediately after minting, so ``now`` really is the mint
    time. Never call it with a token handed back by another cache.
    Callers must hold ``_github_token_cache_lock``.
    """
    now = _utcnow()
    _github_token_cache[project_id] = {
        "token": token,
        "owner": owner,
        "repo": repo,
        "fetched_at": now,
        "expires_at": now + timedelta(seconds=lifetime_seconds),
    }
    remember_project_repo(project_id, owner, repo)

    # Evict entries that are past their true expiry.
    for k in [k for k, v in _github_token_cache.items() if _remaining_life(v) <= 0]:
        _github_token_cache.pop(k, None)


def remember_project_repo(project_id: str, owner: str, repo: str) -> None:
    """Record project -> (owner, repo) so invalidation can reach the repo cache."""
    if owner and repo:
        _project_repo_index[project_id] = (owner, repo)


def forget_project_repo(project_id: str) -> None:
    """Drop a project's cached repo mapping and mint lock.

    Call when the project itself goes away (delete). Without this the mapping
    would survive for the process lifetime, and a deleted project's stale entry
    would keep letting ``invalidate_github_token_cache`` reach into a repo it no
    longer owns — possibly one another project still uses.
    """
    _project_repo_index.pop(project_id, None)
    _project_mint_locks.pop(project_id, None)


def invalidate_github_token_cache(project_id: str) -> None:
    """Force re-generation of a project's GitHub token on next use.

    Drops BOTH caches that can hold a token for this project:

      1. this module's per-project cache (self-minted fallback tokens), and
      2. github_auth's per-repo mint-anchored cache, resolved via
         ``_project_repo_index``.

    (2) is a WIDENING, not a repair of something broken. To be precise about what
    was already covered: ``github.py``'s 401 handler already drops the repo cache
    itself, by regexing ``api.github.com/repos/<owner>/<repo>`` out of the failing
    request URL. The 401-recovery callers in ``rollup_management`` and
    ``rollup_pr_verification`` fail on repo-scoped URLs, so they were already
    covered by that regex and their ``force_refresh=True`` retry did get a fresh
    token. What (2) adds is coverage for the cases that regex CANNOT reach:
    non-repo-scoped URLs (e.g. ``/user``, ``/orgs/...``), and the explicit
    non-401 callers — project delete and project reset.

    BLAST RADIUS: github_auth's cache is keyed per-REPO, not per-project. So
    invalidating project A also forces a re-mint for any OTHER project pointing
    at the same ``owner/repo``. That is correct on a 401 (the token really is
    dead for everyone), and merely a one-off wasted mint on delete/reset.

    This only ever runs on error/eviction paths, so it does not add steady-state
    mint volume — and on those paths a re-mint is exactly what is required.
    """
    removed = _github_token_cache.pop(project_id, None)

    repo_ref = _project_repo_index.get(project_id)
    if repo_ref:
        owner, repo = repo_ref
        # Local import: github_auth imports app.github, which lazily imports this
        # module — importing at module scope would close the cycle.
        from app.github_auth import (
            invalidate_repo_installation_token_cache,
            invalidate_repo_installation_read_token_cache,
        )

        invalidate_repo_installation_token_cache(owner, repo)
        invalidate_repo_installation_read_token_cache(owner, repo)

    if removed or repo_ref:
        logger.info(f"[GITHUB] Invalidated cached token for project {project_id}")


async def get_github_token_for_task(
    task_id: uuid.UUID,
    session: AsyncSession,
    force_refresh: bool = False,
) -> tuple[str, str, str]:
    """Resolve a GitHub token for the task's project.

    The returned token always has at least ``_TOKEN_MIN_REMAINING_LIFE`` seconds
    of life left.

    ``force_refresh`` bypasses this module's per-project cache. It does NOT force
    a re-mint at the github_auth layer — that cache is mint-anchored and already
    guarantees the freshness margin, and forcing re-mints on a periodic caller
    would burn the shared 5,000/hr installation-token budget. To genuinely force
    a re-mint (e.g. after a 401), call ``invalidate_github_token_cache`` first.

    Now that the cache is expiry-correct, ``force_refresh`` is redundant for
    freshness purposes and can be dropped from periodic callers.
    """
    stmt = select(Task).where(Task.id == task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project_id_str = str(task.project_id)

    # Serve a self-minted fallback token only while it retains a real safety margin.
    if not force_refresh:
        async with _github_token_cache_lock:
            cached = get_cached_project_token(project_id_str)
            if cached:
                return cached["token"], cached["owner"], cached["repo"]

    # Column-only SELECT — deliberately NOT an ORM entity load.
    #
    # These three columns are the ONLY ones this function reads. `Project` is a
    # ~390-line model with 15 JSONB columns, and this query now runs on EVERY
    # call for App-discovery projects (they no longer populate the token cache
    # above, so they never short-circuit here). This function has ~60 call sites
    # across 32 modules, including the agent GitHub proxy in
    # routers/agent_github.py — hydrating a JSONB-laden row per call would be a
    # real hot-path regression.
    #
    # A `load_only(..., raiseload=True)` projection would trim the columns too,
    # but it would leave a PARTIALLY-LOADED Project in the session identity map,
    # and that session is shared with the caller. A later `session.get(Project,
    # ...)` without `populate_existing=True` hands back that partial instance,
    # and the first read of an unloaded column raises (or fires a sync lazy-load
    # under async). That is a live bug class here — see the `populate_existing=True`
    # defence written against exactly this in efficiency_snapshot. A column-only
    # SELECT returns a plain Row, never enters the identity map, and therefore
    # cannot poison any of the 60 callers.
    project_stmt = select(
        Project.github_owner, Project.github_repo, Project.user_id
    ).where(Project.id == task.project_id)
    project_row = (await session.execute(project_stmt)).first()
    if not project_row or not project_row.user_id:
        raise HTTPException(status_code=404, detail="Project or user not found")

    owner, repo, project_user_id = project_row

    if not owner or not repo:
        raise HTTPException(
            status_code=403,
            detail="Project does not have an authorized GitHub repository",
        )

    # Preferred: dynamic App-level discovery — always finds the correct
    # installation for this specific repo, even with multiple installations
    # across orgs.
    #
    # Deliberately NOT cached here. get_repo_installation_token owns a
    # mint-anchored, single-flighted per-repo cache; it returns a token with
    # >= 10 min of life left and costs zero GitHub calls while warm. Re-wrapping
    # it in a second TTL anchored at OUR insert time is what allowed an
    # already-old token to be served long past its expiry.
    from app.github import get_repo_installation_token

    try:
        token = await get_repo_installation_token(owner, repo)
    except Exception:  # noqa: BLE001 — fall through to stored-installation + PAT fallbacks
        token = None
        logger.debug(
            f"[GITHUB] App-level discovery failed for {owner}/{repo}, trying fallbacks"
        )

    if token:
        remember_project_repo(project_id_str, owner, repo)
        # NOTE: deliberately NO `_shared_state.last_github_success` stamp here.
        # While github_auth's repo cache is warm this path performs ZERO network
        # I/O — it is a dict lookup. But it is now taken on ~100% of resolver
        # calls for App-discovery projects, and check_github_health() treats a
        # `last_github_success` younger than 60s as proof GitHub is up and SKIPS
        # its active probe. Stamping here would therefore re-assert "GitHub is
        # healthy" from an in-memory dict on every call, holding the health check
        # green for up to the full 50-min cache window into a real outage.
        # Only a genuine round-trip may stamp it.
        return token, owner, repo

    # Fallbacks. These have no other cache, so THIS module must cache them or a
    # sweep would mint (or hit Secret Manager) on every single call. Because we
    # mint them right here, insert time == mint time and expires_at is sound.
    #
    # Single-flighted PER PROJECT: the mints below are network I/O performed
    # outside _github_token_cache_lock, so without this a concurrent cold-cache
    # burst on one project would fire one mint per caller at the shared
    # 5,000/hr bucket.
    async with _get_project_mint_lock(project_id_str):
        if not force_refresh:
            # Another coroutine may have minted while we waited for the lock.
            async with _github_token_cache_lock:
                cached = get_cached_project_token(project_id_str)
            if cached:
                return cached["token"], cached["owner"], cached["repo"]

        integration_stmt = select(UserIntegration).where(
            UserIntegration.user_id == project_user_id
        )
        integration = (await session.execute(integration_stmt)).scalars().first()

        if integration and integration.github_installation_id:
            try:
                token = await generate_installation_access_token(
                    integration.github_installation_id, repositories=[repo]
                )
            except Exception:  # noqa: BLE001 — fall through to PAT fallback
                logger.debug(
                    f"[GITHUB] Stored installation token failed for {owner}/{repo}"
                )

        if not token and integration and integration.github_pat:
            decrypted_pat_ref = decrypt_field(integration.github_pat)
            token = await get_user_secret(decrypted_pat_ref)

        if not token:
            raise HTTPException(
                status_code=400,
                detail="GitHub integration not found. Ensure the GitHub App is installed on this repository, or register a PAT via /api/github/pat.",
            )

        # A token was just obtained through a real network round-trip (the
        # installation-token mint, or Secret Manager on the PAT path) rather than
        # from a warm in-memory cache, so this IS a live reachability signal and
        # may legitimately stamp the passive health marker. Contrast the
        # App-discovery return above.
        _shared_state.last_github_success = _utcnow()

        async with _github_token_cache_lock:
            cache_project_token(project_id_str, token, owner, repo)

    return token, owner, repo
