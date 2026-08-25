"""Cached branch-tree snapshots for grounding/validation GitHub reads.

The citation-grounding machinery (``app.source_ref_validation``) historically
answered "does this path exist on the integration branch?" and "what is this
file's content?" with one ``GET /contents/<path>`` call per cited file, per
pass, with no caching. A single design-doc grounding pass over a large doc
could fire 1,000+ API calls and drain the 5,000/hr GitHub App installation
bucket that PR opens, merges, and CI reads also depend on.

This module replaces that fan-out with snapshot reads:

* **Branch tip** — ``(owner, repo, branch) → commit SHA``, cached for a short
  TTL. One ``git/ref`` call per repo/branch per TTL window.
* **Tree snapshot** — ``commit SHA → {path → blob SHA}`` via one
  ``git/trees/{sha}?recursive=1`` call. A commit's tree is immutable, so the
  snapshot is cached by SHA (small LRU). Existence checks become set
  membership: O(cited files) API calls collapse to ≤2 per pass, shared across
  concurrent passes by a single-flight lock.
* **Blob content** — ``blob SHA → decoded text`` via ``git/blobs/{sha}``,
  cached in a byte-bounded LRU. Blobs are immutable, so a cached entry never
  goes stale; hot files (symbol grounding re-reads the same modules
  constantly) cost one fetch per content change, fleet-wide.

Degradation is conservative, mirroring ``source_ref_validation``'s tri-state
semantics: any API failure, a truncated tree, or the quota reserve floor
yields *indeterminate* (``None``), never a definite absence — so a rate-limit
event can no longer masquerade as "the cited file was deleted".

**Quota reserve floor**: grounding is advisory by construction (an
indeterminate check leaves state untouched), while the same installation
bucket funds critical pipeline operations (PR opens, merges, CI status). When
the observed install-bucket remaining drops below
:data:`GROUNDING_QUOTA_RESERVE`, every snapshot read short-circuits to
indeterminate without a network call. Validation quality degrades for a few
minutes; the pipeline keeps merging.
"""

import asyncio
import base64
import fnmatch
import io
import logging
import tarfile
import time
from collections import OrderedDict
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# Below this many observed remaining install-bucket requests, grounding reads
# stop spending quota entirely and degrade to indeterminate. Sized so the
# critical PR/merge/CI machinery always has headroom even if a grounding storm
# and a merge wave coincide.
GROUNDING_QUOTA_RESERVE = 500

# Branch-tip SHA cache TTL. Grounding does not need to-the-second freshness —
# it validates citations against "the integration branch as of about now".
_BRANCH_SHA_TTL_S = 60.0

# Trees are cached by commit SHA (immutable), so the only reason to evict is
# memory. A handful of (repo, branch) combos are live at any time.
_TREE_CACHE_MAX_ENTRIES = 8

# Blob text cache: byte-bounded LRU. Blobs are immutable → entries never stale.
_BLOB_CACHE_MAX_BYTES = 24 * 1024 * 1024
_BLOB_CACHE_MAX_ENTRIES = 512

# Authoritative merge reads download the same immutable repository archive for
# every preflight that examines a given commit. Cache the compressed response
# bytes, not extracted text: callers request different path sets and may use
# different ``allow_missing_paths`` contracts, so extraction must remain
# request-local. Keep this cache deliberately small because one archive is
# commonly ~20 MiB compressed.
_ARCHIVE_CACHE_MAX_BYTES = 96 * 1024 * 1024
_ARCHIVE_CACHE_MAX_ENTRIES = 4

_branch_sha_cache: Dict[Tuple[str, str, str], Tuple[str, float]] = {}
# (owner, repo, commit_sha) → (paths {path → blob_sha}, truncated)
_tree_cache: "OrderedDict[Tuple[str, str, str], Tuple[Dict[str, str], bool]]" = (
    OrderedDict()
)
_blob_cache: "OrderedDict[str, str]" = OrderedDict()
_blob_cache_bytes = 0
_archive_cache: "OrderedDict[Tuple[str, str, str], bytes]" = OrderedDict()
_archive_cache_bytes = 0
# Single-flight: concurrent grounding passes share one tree fetch per branch.
_tree_fetch_locks: Dict[Tuple[str, str, str], asyncio.Lock] = {}
# A completed task is removed by its own callback, so waiters that already
# acquired this exact flight continue to share its success or failure.
_archive_fetch_tasks: Dict[Tuple[str, str, str], asyncio.Task[bytes | None]] = {}


def _bump_cache_stat(key: str) -> None:
    import app.shared_state as _ss

    _ss.github_cache_hits[key] = _ss.github_cache_hits.get(key, 0) + 1


def _quota_reserve_active() -> bool:
    """Whether grounding reads should stop spending install-bucket quota.

    True only when we have OBSERVED the bucket low (remaining below the
    reserve) with a reset still in the future. An untracked/never-observed
    bucket (e.g. right after boot) never blocks — we only gate on evidence.
    """
    import app.shared_state as _ss

    q = _ss.github_quota
    remaining = q.get("remaining")
    if remaining is None or remaining >= GROUNDING_QUOTA_RESERVE:
        return False
    reset_at = q.get("reset_at")
    if reset_at is not None and int(reset_at) <= int(time.time()):
        # The window already rolled over; the stale low reading is meaningless.
        return False
    _bump_cache_stat("grounding_reserve_skip")
    return True


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _resolve_branch_sha(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> str | None:
    """Branch tip SHA, cached for :data:`_BRANCH_SHA_TTL_S`. None on failure."""
    key = (owner, repo, branch)
    now = time.monotonic()
    cached = _branch_sha_cache.get(key)
    if cached is not None and (now - cached[1]) < _BRANCH_SHA_TTL_S:
        _bump_cache_stat("branch_sha_snapshot")
        return cached[0]

    from urllib.parse import quote

    from app.github import _github_request

    url = (
        f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/"
        f"{quote(branch, safe='')}"
    )
    try:
        response = await _github_request(
            "GET", url, _headers(token), max_retries=2, project_id=project_id
        )
        if response.status_code != 200:
            logger.warning(
                "[REPO_SNAPSHOT] branch tip lookup for %s/%s@%s: HTTP %s",
                owner,
                repo,
                branch,
                response.status_code,
            )
            return None
        sha = (response.json().get("object") or {}).get("sha")
        if not isinstance(sha, str) or not sha:
            return None
        _branch_sha_cache[key] = (sha, now)
        return sha
    except Exception as e:  # noqa: BLE001 — snapshot reads must degrade, not raise
        logger.warning(
            "[REPO_SNAPSHOT] branch tip lookup failed for %s/%s@%s: %s",
            owner,
            repo,
            branch,
            e,
        )
        return None


async def _fetch_tree(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    project_id: str | None = None,
) -> Tuple[Dict[str, str], bool] | None:
    """Fetch the recursive tree for *sha*. Returns (paths, truncated) or None."""
    from app.github import _github_request

    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{sha}?recursive=1"
    try:
        response = await _github_request(
            "GET", url, _headers(token), max_retries=2, project_id=project_id
        )
        if response.status_code != 200:
            logger.warning(
                "[REPO_SNAPSHOT] tree fetch for %s/%s@%s: HTTP %s",
                owner,
                repo,
                sha[:12],
                response.status_code,
            )
            return None
        payload = response.json()
        entries = payload.get("tree")
        if not isinstance(entries, list):
            return None
        paths: Dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            path = entry.get("path")
            blob_sha = entry.get("sha")
            if isinstance(path, str) and isinstance(blob_sha, str):
                paths[path] = blob_sha
        truncated = bool(payload.get("truncated"))
        if truncated:
            logger.warning(
                "[REPO_SNAPSHOT] tree for %s/%s@%s is truncated (%d entries) — "
                "absent paths will degrade to indeterminate",
                owner,
                repo,
                sha[:12],
                len(paths),
            )
        return paths, truncated
    except Exception as e:  # noqa: BLE001 — snapshot reads must degrade, not raise
        logger.warning(
            "[REPO_SNAPSHOT] tree fetch failed for %s/%s@%s: %s",
            owner,
            repo,
            sha[:12],
            e,
        )
        return None


async def _get_snapshot(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> Tuple[Dict[str, str], bool] | None:
    """Resolve the cached (paths, truncated) snapshot for *branch*, or None."""
    if _quota_reserve_active():
        return None
    sha = await _resolve_branch_sha(token, owner, repo, branch, project_id)
    if not sha:
        return None
    return await _get_snapshot_at_sha(token, owner, repo, sha, project_id)


async def _get_snapshot_at_sha(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    project_id: str | None = None,
) -> Tuple[Dict[str, str], bool] | None:
    """The cached (paths, truncated) snapshot for an EXPLICIT commit *sha*.

    A commit's tree is immutable, so this read has no freshness window at all —
    unlike branch reads, which resolve the tip through a
    :data:`_BRANCH_SHA_TTL_S` cache and can lag the branch by up to that TTL.
    Callers that know the exact commit they mean (e.g. corroborating a merged
    PR against its merge commit) MUST use this instead of a branch read.
    """
    if _quota_reserve_active():
        return None
    key = (owner, repo, sha)
    snapshot = _tree_cache.get(key)
    if snapshot is not None:
        _tree_cache.move_to_end(key)
        _bump_cache_stat("tree_snapshot")
        return snapshot
    lock = _tree_fetch_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            # Re-check after the wait: a concurrent pass may have populated it.
            snapshot = _tree_cache.get(key)
            if snapshot is not None:
                _tree_cache.move_to_end(key)
                _bump_cache_stat("tree_snapshot")
                return snapshot
            snapshot = await _fetch_tree(token, owner, repo, sha, project_id)
            if snapshot is None:
                return None
            _tree_cache[key] = snapshot
            while len(_tree_cache) > _TREE_CACHE_MAX_ENTRIES:
                _tree_cache.popitem(last=False)
            return snapshot
    finally:
        _tree_fetch_locks.pop(key, None)


def _blob_cache_get(blob_sha: str) -> str | None:
    text = _blob_cache.get(blob_sha)
    if text is not None:
        _blob_cache.move_to_end(blob_sha)
        _bump_cache_stat("blob_snapshot")
    return text


def _blob_cache_put(blob_sha: str, text: str) -> None:
    global _blob_cache_bytes
    size = len(text)
    if size > _BLOB_CACHE_MAX_BYTES:
        return
    existing = _blob_cache.pop(blob_sha, None)
    if existing is not None:
        _blob_cache_bytes -= len(existing)
    _blob_cache[blob_sha] = text
    _blob_cache_bytes += size
    while _blob_cache and (
        _blob_cache_bytes > _BLOB_CACHE_MAX_BYTES
        or len(_blob_cache) > _BLOB_CACHE_MAX_ENTRIES
    ):
        _, evicted = _blob_cache.popitem(last=False)
        _blob_cache_bytes -= len(evicted)


def _archive_cache_get(key: Tuple[str, str, str]) -> bytes | None:
    archive = _archive_cache.get(key)
    if archive is not None:
        _archive_cache.move_to_end(key)
        _bump_cache_stat("authoritative_archive")
    return archive


def _archive_cache_put(key: Tuple[str, str, str], archive: bytes) -> None:
    global _archive_cache_bytes
    size = len(archive)
    if size > _ARCHIVE_CACHE_MAX_BYTES:
        logger.warning(
            "[REPO_SNAPSHOT] authoritative archive too large to cache for %s/%s@%s: %d bytes (limit %d)",
            key[0],
            key[1],
            key[2][:12],
            size,
            _ARCHIVE_CACHE_MAX_BYTES,
        )
        return
    existing = _archive_cache.pop(key, None)
    if existing is not None:
        _archive_cache_bytes -= len(existing)
    _archive_cache[key] = archive
    _archive_cache_bytes += size
    while _archive_cache and (
        _archive_cache_bytes > _ARCHIVE_CACHE_MAX_BYTES
        or len(_archive_cache) > _ARCHIVE_CACHE_MAX_ENTRIES
    ):
        _, evicted = _archive_cache.popitem(last=False)
        _archive_cache_bytes -= len(evicted)


async def _fetch_authoritative_archive(
    *,
    token: str,
    owner: str,
    repo: str,
    commit_sha: str,
    project_id: str | None,
) -> bytes | None:
    """Fetch and admit one immutable archive, returning None on failure."""
    from urllib.parse import quote

    from app.github import _github_request

    cache_key = (owner, repo, commit_sha)
    cached = _archive_cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{quote(commit_sha, safe='')}"
    try:
        response = await _github_request(
            "GET",
            url,
            _headers(token),
            max_retries=2,
            project_id=project_id,
            follow_redirects=True,
        )
        if response.status_code != 200:
            logger.warning(
                "[REPO_SNAPSHOT] authoritative archive fetch for %s/%s@%s: HTTP %s",
                owner,
                repo,
                commit_sha[:12],
                response.status_code,
            )
            return None
        archive = response.content
        _archive_cache_put(cache_key, archive)
        return archive
    except Exception as e:  # noqa: BLE001 — merge reads must degrade, not raise
        logger.warning(
            "[REPO_SNAPSHOT] authoritative archive fetch failed for %s/%s@%s: %s",
            owner,
            repo,
            commit_sha[:12],
            e,
        )
        return None


async def _fetch_blob_text(
    token: str,
    owner: str,
    repo: str,
    blob_sha: str,
    project_id: str | None = None,
) -> str | None:
    from app.github import _github_request

    url = f"https://api.github.com/repos/{owner}/{repo}/git/blobs/{blob_sha}"
    try:
        response = await _github_request(
            "GET", url, _headers(token), max_retries=2, project_id=project_id
        )
        if response.status_code != 200:
            logger.warning(
                "[REPO_SNAPSHOT] blob fetch for %s/%s blob %s: HTTP %s",
                owner,
                repo,
                blob_sha[:12],
                response.status_code,
            )
            return None
        payload = response.json()
        content_b64 = payload.get("content")
        if not isinstance(content_b64, str):
            return None
        return base64.b64decode(content_b64).decode(errors="replace")
    except Exception as e:  # noqa: BLE001 — snapshot reads must degrade, not raise
        logger.warning(
            "[REPO_SNAPSHOT] blob fetch failed for %s/%s blob %s: %s",
            owner,
            repo,
            blob_sha[:12],
            e,
        )
        return None


async def authoritative_texts_at_commit(
    paths: set[str],
    *,
    token: str,
    owner: str,
    repo: str,
    commit_sha: str,
    project_id: str | None = None,
    allow_missing_paths: bool = False,
) -> dict[str, str] | None:
    """Read several paths from one exact-SHA archive for a merge decision.

    This is deliberately separate from the advisory grounding snapshot APIs
    above: merge authority must not yield to ``GROUNDING_QUOTA_RESERVE`` or a
    branch-tip cache.  GitHub's tarball endpoint accepts an immutable commit
    SHA, so one request supplies a coherent tree without a Contents-API fanout.
    ``None`` means the archive or a member's text could not be read and callers
    must fail closed. ``allow_missing_paths`` preserves an exact tree's known
    absences for baseline comparison; it never turns an archive failure into an
    absence.
    """
    if not paths or any(not path or path.startswith("/") or ".." in path.split("/") for path in paths):
        return None
    cache_key = (owner, repo, commit_sha)
    try:
        archive_bytes = _archive_cache_get(cache_key)
        if archive_bytes is None:
            flight = _archive_fetch_tasks.get(cache_key)
            if flight is None:
                flight = asyncio.create_task(
                    _fetch_authoritative_archive(
                        token=token,
                        owner=owner,
                        repo=repo,
                        commit_sha=commit_sha,
                        project_id=project_id,
                    )
                )
                _archive_fetch_tasks[cache_key] = flight

                def _clear_completed_flight(done_task: asyncio.Task[bytes | None]) -> None:
                    if _archive_fetch_tasks.get(cache_key) is done_task:
                        _archive_fetch_tasks.pop(cache_key, None)

                flight.add_done_callback(_clear_completed_flight)
            # Shield the shared fetch from cancellation by one extraction
            # caller; other callers still need its immutable result.
            archive_bytes = await asyncio.shield(flight)

        if archive_bytes is None:
            return None

        def _extract() -> dict[str, str] | None:
            try:
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
                    texts: dict[str, str] = {}
                    for member in archive:
                        if not member.isfile():
                            continue
                        member_path = member.name.split("/", 1)
                        if len(member_path) != 2 or member_path[1] not in paths:
                            continue
                        handle = archive.extractfile(member)
                        if handle is None:
                            return None
                        texts[member_path[1]] = handle.read().decode("utf-8", errors="replace")
                    return texts if allow_missing_paths or set(texts) == paths else None
            except (tarfile.TarError, OSError, UnicodeError):
                return None

        return await asyncio.to_thread(_extract)
    except Exception:  # noqa: BLE001 — merge callers turn unavailable evidence into defer
        logger.warning(
            "[REPO_SNAPSHOT] authoritative archive read failed for %s/%s@%s",
            owner,
            repo,
            commit_sha[:12],
            exc_info=True,
        )
        return None


async def path_exists_on_branch(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> bool | None:
    """Whether *path* is a file on ``owner/repo@branch``. Tri-state.

    True/False answer set membership against the cached tree snapshot; None is
    indeterminate (snapshot unavailable, quota reserve active, or the tree was
    truncated and the path was not in the visible portion). Callers MUST treat
    None conservatively — it is never evidence of absence.
    """
    snapshot = await _get_snapshot(token, owner, repo, branch, project_id)
    if snapshot is None:
        return None
    paths, truncated = snapshot
    if path in paths:
        return True
    return None if truncated else False


async def glob_exists_on_branch(
    pattern: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> bool | None:
    """Whether *pattern* matches any file on ``owner/repo@branch``. Tri-state.

    True when the cached branch tree contains at least one matching blob path.
    False only when the tree is complete and no path matches. None means the
    snapshot is unavailable or truncated with no visible match, so absence
    cannot be proven.
    """
    snapshot = await _get_snapshot(token, owner, repo, branch, project_id)
    if snapshot is None:
        return None
    paths, truncated = snapshot
    if fnmatch.filter(list(paths.keys()), pattern):
        return True
    return None if truncated else False


async def list_dir_children_on_branch(
    dir_path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> list | None:
    """Immediate children of *dir_path* derived from the cached flat tree.

    Returns a list of ``{name, type, path}`` dicts that mirror the shape of
    GitHub's ``GET /repos/.../contents/<dir>`` response (only the fields
    consumed by :func:`_detect_build_config` in ``admin_onboard``).  Type is
    ``"file"`` for blobs and ``"dir"`` for inferred intermediate directories.

    Returns ``None`` when the snapshot is unavailable or truncated — callers
    MUST fall back to the direct GitHub Contents API on ``None`` to preserve
    the detector's conservative degradation semantics (indeterminate must never
    be treated as absent).
    """
    snapshot = await _get_snapshot(token, owner, repo, branch, project_id)
    if snapshot is None:
        return None
    paths, truncated = snapshot
    if truncated:
        # A truncated tree would produce an incomplete listing — fall back to
        # the direct API so the detector never silently misses files.
        return None

    # Build a prefix to match direct children of dir_path.
    prefix = f"{dir_path}/" if dir_path and dir_path not in ("", ".") else ""
    children: Dict[str, str] = {}  # name -> type
    for p in paths:
        if prefix:
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
        else:
            rest = p
        if not rest:
            continue
        slash_idx = rest.find("/")
        if slash_idx == -1:
            # Direct child file
            children[rest] = "file"
        else:
            # File inside a subdirectory — first component is the dir child
            child_name = rest[:slash_idx]
            if child_name not in children:
                children[child_name] = "dir"

    return [
        {
            "name": name,
            "type": typ,
            "path": f"{prefix}{name}" if prefix else name,
        }
        for name, typ in sorted(children.items())
    ]


async def file_text_on_branch(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> Tuple[bool | None, str | None]:
    """Decoded text of *path* on ``owner/repo@branch`` via cached blob reads.

    Returns ``(exists, text)`` with tri-state ``exists`` matching
    :func:`path_exists_on_branch`. ``text`` is None whenever it cannot be
    determined; a present file whose blob fetch fails yields ``(True, None)``
    so callers can distinguish "absent" from "unreadable".
    """
    snapshot = await _get_snapshot(token, owner, repo, branch, project_id)
    return await _file_text_from_snapshot(snapshot, path, token, owner, repo, project_id)


async def file_text_at_commit(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    commit_sha: str,
    project_id: str | None = None,
) -> Tuple[bool | None, str | None]:
    """Decoded text of *path* in the tree of an EXPLICIT commit. Tri-state.

    Same return contract as :func:`file_text_on_branch`, but reads the immutable
    tree of *commit_sha* directly — no branch-tip resolution, so no
    :data:`_BRANCH_SHA_TTL_S` staleness window. Use this whenever the caller
    knows the exact commit the answer must be true at (e.g. a merged PR's merge
    commit): a branch read taken moments after a merge can still serve the
    PRE-merge tip from cache and report a just-added file absent.
    """
    snapshot = await _get_snapshot_at_sha(token, owner, repo, commit_sha, project_id)
    return await _file_text_from_snapshot(snapshot, path, token, owner, repo, project_id)


async def _file_text_from_snapshot(
    snapshot: Tuple[Dict[str, str], bool] | None,
    path: str,
    token: str,
    owner: str,
    repo: str,
    project_id: str | None = None,
) -> Tuple[bool | None, str | None]:
    if snapshot is None:
        return None, None
    paths, truncated = snapshot
    blob_sha = paths.get(path)
    if blob_sha is None:
        return (None, None) if truncated else (False, None)
    text = _blob_cache_get(blob_sha)
    if text is not None:
        return True, text
    if _quota_reserve_active():
        return True, None
    text = await _fetch_blob_text(token, owner, repo, blob_sha, project_id)
    if text is None:
        return True, None
    _blob_cache_put(blob_sha, text)
    return True, text
