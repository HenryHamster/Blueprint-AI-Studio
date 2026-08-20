"""Narrow machinery-only candidate materialization for the hotfix-promote lane.

WHY THIS EXISTS (knowledge 0cee18f3, direction "engine-driven promotion").
:mod:`app.hotfix_policy`'s ``evaluate_hotfix_scope`` is a correct, fail-closed
predicate — the problem was always its INPUT.  ``hotfix_promote_current_version``
fed it a GitHub compare between the promotion-target HEAD and the integration
HEAD, i.e. EVERY commit integration has accumulated since the last promotion.  In
the exact situation the lane exists for — a latched gate whose repair is already
sitting in integration's history — that compare is hundreds of product files, so
the lane returned ``scope_rejected`` for a two-file release-machinery fix and the
operator had to hand-cut a branch.

This module closes that gap WITHOUT weakening authorization:

  * the operator NAMES the repair as commit SHAs (``repair_commit_shas``) —
    the same "declare, then independently verify" shape as ``scope_modules``;
  * each named commit is diffed against ITS OWN first parent, so the file set is
    the repair's, not the accumulated candidate's;
  * the UNMODIFIED :func:`app.hotfix_policy.evaluate_hotfix_scope` runs over that
    union and still rejects fail-closed on anything unrecognized — nothing is
    silently filtered out of a wide diff;
  * an eligible repair is materialized as a NEW commit parented on the
    promotion-target HEAD carrying ONLY those paths, with content taken from the
    repair's own tree (existing blob SHAs — byte-exact, no re-encode).

``promote()`` ships exactly the SHA it is handed, so a narrow SHA is the only way
to ship a narrow change through the existing engine.  The materialized commit
moves NO ref: ``promote()``'s own ``create_branch`` is what first points a ref at
it.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import github, hotfix_policy, release_version
from app.models import Project

logger = logging.getLogger(__name__)

# A named repair commit must be a full 40-hex SHA: an abbreviated or symbolic ref
# is ambiguous, and this value selects what gets DEPLOYED.
_REPAIR_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# The lane ships a SURGICAL repair.  A request naming more commits than this is
# not a surgical repair; route it through the full gate.
MAX_REPAIR_COMMITS = 10


def _release_qualifier():
    """Resolve the qualification engine only after both modules initialize."""
    from app import release_qualifier

    return release_qualifier


def _scope_verdict_for_entries(
    entries: list[dict],
) -> hotfix_policy.HotfixScopeVerdict:
    """Run the UNMODIFIED fail-closed scope predicate over compare ``entries``."""
    changed_files = [e["filename"] for e in entries]
    main_py_patch = next(
        (
            e.get("patch")
            for e in entries
            if e["filename"] == hotfix_policy.MAIN_PY_PATH
        ),
        None,
    )
    return hotfix_policy.evaluate_hotfix_scope(
        changed_files, main_py_patch=main_py_patch
    )


async def _union_repair_diff(
    token: str,
    owner: str,
    repo: str,
    repair_commit_shas: list[str],
    *,
    project_id: str | None = None,
) -> list[dict] | None:
    """Union each named commit's OWN diff (against its first parent).

    Returns compare entries (``{"filename", "patch"}``) ordered by first
    appearance, with a later-named commit's entry winning for a path touched more
    than once.  Returns ``None`` when ANY named commit's diff is indeterminate —
    an unresolvable diff must never read as "no offending files".
    """
    from app import github_refs

    merged: dict[str, dict] = {}
    for sha in repair_commit_shas:
        entries = await github_refs.get_changed_files_with_patches_between_refs(
            token, owner, repo, f"{sha}~1", sha, project_id=project_id
        )
        if entries is None:
            logger.warning(
                "[HOTFIX_NARROW] could not resolve the own-diff of repair commit "
                "%s on %s/%s — refusing fail-closed",
                sha[:8],
                owner,
                repo,
            )
            return None
        for entry in entries:
            merged[entry["filename"]] = entry
    return list(merged.values())


def _validate_repair_commit_shas(repair_commit_shas: list[str]) -> str | None:
    """Return a rejection reason for a malformed commit list, else ``None``."""
    if not repair_commit_shas:
        return "no repair commits named — nothing to materialize (fail-closed)"
    if len(repair_commit_shas) > MAX_REPAIR_COMMITS:
        return (
            f"{len(repair_commit_shas)} repair commits named (max "
            f"{MAX_REPAIR_COMMITS}) — that is not a surgical repair; route it "
            "through the full gate"
        )
    bad = [s for s in repair_commit_shas if not _REPAIR_SHA_RE.match(s or "")]
    if bad:
        return (
            "repair commits must be full 40-hex SHAs (an abbreviated or symbolic "
            f"ref is ambiguous about what deploys): {bad}"
        )
    return None


async def narrow_repair_scope(
    token: str,
    owner: str,
    repo: str,
    repair_commit_shas: list[str],
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Scope-check the NAMED repair commits' own diffs (no mutation).

    Returns ``{"status": "eligible"|"scope_rejected"|"diff_unavailable",
    "changed_files", "offending_files", "reason", "entries"}``.  ``entries`` is
    the union compare payload, carried so the caller does not re-fetch it.
    """
    reason = _validate_repair_commit_shas(repair_commit_shas)
    if reason is not None:
        return {
            "status": "scope_rejected",
            "changed_files": [],
            "offending_files": [],
            "reason": reason,
            "entries": [],
        }
    entries = await _union_repair_diff(
        token, owner, repo, repair_commit_shas, project_id=project_id
    )
    if entries is None:
        return {
            "status": "diff_unavailable",
            "changed_files": [],
            "offending_files": [],
            "reason": (
                "could not resolve the named repair commits' own diffs "
                "(GitHub compare failed) — refusing fail-closed"
            ),
            "entries": [],
        }
    verdict = _scope_verdict_for_entries(entries)
    return {
        "status": "eligible" if verdict.eligible else "scope_rejected",
        "changed_files": [e["filename"] for e in entries],
        "offending_files": verdict.offending_files,
        "reason": verdict.reason,
        "entries": entries,
    }


async def _tree_entries_for_paths(
    token: str,
    owner: str,
    repo: str,
    paths: list[str],
    *,
    content_ref: str,
    prior_deployed_sha: str,
    project_id: str | None = None,
) -> list[dict] | None:
    """Build Git-tree entries carrying ``paths``' content from ``content_ref``.

    Reuses the EXISTING blob objects (``get_file_blob_sha_at_ref``) rather than
    re-uploading decoded text, so the shipped bytes are exactly the bytes the
    repair commit tested.  A path absent at ``content_ref`` becomes a deletion
    (``sha: None``) — but only when it actually exists on the promotion target,
    since deleting an already-absent path is a no-op GitHub rejects.

    Returns ``None`` on ANY indeterminate lookup: content that cannot be resolved
    must not silently ship or silently drop.
    """
    tree_entries: list[dict] = []
    for path in paths:
        exists, blob_sha = await github.get_file_blob_sha_at_ref(
            path,
            token=token,
            owner=owner,
            repo=repo,
            ref=content_ref,
            project_id=project_id,
        )
        if exists is None or (exists and not blob_sha):
            logger.warning(
                "[HOTFIX_NARROW] indeterminate content for %s at %s — refusing "
                "fail-closed",
                path,
                content_ref[:8],
            )
            return None
        if exists:
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
            )
            continue
        base_exists, _ = await github.get_file_blob_sha_at_ref(
            path,
            token=token,
            owner=owner,
            repo=repo,
            ref=prior_deployed_sha,
            project_id=project_id,
        )
        if base_exists is None:
            logger.warning(
                "[HOTFIX_NARROW] indeterminate base state for deleted path %s — "
                "refusing fail-closed",
                path,
            )
            return None
        if base_exists:
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": None}
            )
    return tree_entries


async def materialize_narrow_hotfix_candidate(
    token: str,
    owner: str,
    repo: str,
    *,
    prior_deployed_sha: str,
    repair_commit_shas: list[str],
    content_ref: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Build a machinery-only candidate commit on top of ``prior_deployed_sha``.

    Steps: union the named commits' own diffs -> run the unmodified fail-closed
    scope check -> reuse each surviving path's blob at ``content_ref`` (default:
    the LAST named repair commit) -> create ONE commit parented on
    ``prior_deployed_sha``.  No ref is created or moved.

    Returns one of::

        {"status": "scope_rejected",   "offending_files": [...], "reason": ...}
        {"status": "diff_unavailable", "reason": ...}
        {"status": "content_unavailable", "reason": ...}
        {"status": "no_effective_change", "reason": ...}
        {"status": "commit_failed",    "reason": ...}
        {"status": "materialized", "candidate_sha": ..., "changed_files": [...],
         "content_ref": ..., "repair_commit_shas": [...]}
    """
    scope = await narrow_repair_scope(
        token, owner, repo, repair_commit_shas, project_id=project_id
    )
    if scope["status"] != "eligible":
        return {
            "status": scope["status"],
            "offending_files": scope["offending_files"],
            "changed_files": scope["changed_files"],
            "reason": scope["reason"],
        }
    source_ref = content_ref or repair_commit_shas[-1]
    paths = scope["changed_files"]
    tree_entries = await _tree_entries_for_paths(
        token,
        owner,
        repo,
        paths,
        content_ref=source_ref,
        prior_deployed_sha=prior_deployed_sha,
        project_id=project_id,
    )
    if tree_entries is None:
        return {
            "status": "content_unavailable",
            "offending_files": [],
            "changed_files": paths,
            "reason": (
                "could not resolve the repair's file content at "
                f"{source_ref[:8]} — refusing fail-closed"
            ),
        }
    if not tree_entries:
        return {
            "status": "no_effective_change",
            "offending_files": [],
            "changed_files": paths,
            "reason": (
                "the named repair commits change nothing relative to the "
                "promotion target — nothing to hotfix-deploy"
            ),
        }
    message = (
        "hotfix: narrow release-machinery candidate\n\n"
        f"Materialized from repair commit(s) {', '.join(repair_commit_shas)} "
        f"on top of {prior_deployed_sha[:12]}.\n"
        f"Paths: {', '.join(paths)}"
    )
    try:
        narrow_sha = await github.create_dangling_commit_from_blob_refs(
            token,
            owner,
            repo,
            base_ref=prior_deployed_sha,
            tree_entries=tree_entries,
            message=message,
            project_id=project_id,
        )
    except github.DanglingCommitUnchangedError as exc:
        return {
            "status": "no_effective_change",
            "offending_files": [],
            "changed_files": paths,
            "reason": str(exc)[:300],
        }
    except Exception as exc:  # noqa: BLE001 — a GitHub write failure is recoverable
        logger.warning(
            "[HOTFIX_NARROW] failed to materialize narrow candidate on %s/%s: %s",
            owner,
            repo,
            exc,
        )
        return {
            "status": "commit_failed",
            "offending_files": [],
            "changed_files": paths,
            "reason": f"{type(exc).__name__}: {exc}"[:300],
        }
    logger.warning(
        "[HOTFIX_NARROW] materialized narrow candidate %s on %s/%s@%s from "
        "repair commits %s — paths=%s",
        narrow_sha[:12],
        owner,
        repo,
        prior_deployed_sha[:12],
        [s[:8] for s in repair_commit_shas],
        paths,
    )
    return {
        "status": "materialized",
        "candidate_sha": narrow_sha,
        "changed_files": paths,
        "offending_files": [],
        "content_ref": source_ref,
        "repair_commit_shas": list(repair_commit_shas),
        "reason": "all named repair commits are allowlisted release machinery",
    }


async def check_hotfix_scope_preview(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_sha: str | None = None,
    repair_commit_shas: list[str] | None = None,
) -> dict[str, Any]:
    """Read-only preview: report whether a hotfix-eligible change is shippable
    WITHOUT promoting.

    Returns a dict with keys:
      ``status``         — one of 'eligible', 'ineligible', 'no_open_version',
                           'no_credentials', 'sha_error', 'diff_unavailable'
      ``eligible``       — bool (present when status is 'eligible' or 'ineligible')
      ``changed_files``  — list of all changed file paths (present on success)
      ``offending_files``— list of paths that failed the scope check (empty if eligible)
      ``reason``         — human-readable verdict string (present on success)
      ``candidate_sha``  — resolved candidate SHA (present on success)
      ``prior_deployed_sha`` — promotion-target HEAD SHA (present on success)
      ``repair_commit_shas`` — echo of the named repair commits (narrow mode)

    With ``repair_commit_shas`` the preview evaluates the NARROW path: the union
    of those commits' own diffs, i.e. exactly what ``POST /promote/hotfix`` would
    materialize and ship.  Without it, it evaluates the whole accumulated
    promotion-target..candidate compare (the original behaviour).

    Makes NO mutations: no commit is created, no branch is cut, no PR is opened,
    no audit trail is written.
    """
    version = await release_version.current_open_version(session, project_id)
    if version is None:
        return {"status": "no_open_version"}
    project = await session.get(Project, project_id)
    if project is None:
        return {"status": "no_open_version"}
    _rq = _release_qualifier()
    resolved = await _rq._resolve_github_with_session(session, project)
    if resolved is None:
        return {"status": "no_credentials"}
    token, owner, repo = resolved
    integration_branch, promotion_target = _rq._lts_branch_names(project)
    if not candidate_sha:
        try:
            candidate_sha = await github.get_branch_sha(
                token, owner, repo, integration_branch, live=True
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "sha_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not candidate_sha:
        return {"status": "sha_error", "detail": "empty candidate sha"}
    try:
        prior_deployed_sha = await github.get_branch_sha(
            token, owner, repo, promotion_target, live=True
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "sha_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not prior_deployed_sha:
        return {"status": "sha_error", "detail": "empty promotion-target sha"}

    if repair_commit_shas:
        scope = await narrow_repair_scope(
            token, owner, repo, list(repair_commit_shas), project_id=str(project_id)
        )
        if scope["status"] == "diff_unavailable":
            return {
                "status": "diff_unavailable",
                "eligible": False,
                "changed_files": [],
                "offending_files": [],
                "reason": scope["reason"],
                "candidate_sha": candidate_sha,
                "prior_deployed_sha": prior_deployed_sha,
                "repair_commit_shas": list(repair_commit_shas),
            }
        eligible = scope["status"] == "eligible"
        return {
            "status": "eligible" if eligible else "ineligible",
            "eligible": eligible,
            "changed_files": scope["changed_files"],
            "offending_files": scope["offending_files"],
            "reason": scope["reason"],
            "candidate_sha": candidate_sha,
            "prior_deployed_sha": prior_deployed_sha,
            "repair_commit_shas": list(repair_commit_shas),
        }

    from app import github_refs

    changed = await github_refs.get_changed_files_with_patches_between_refs(
        token, owner, repo, prior_deployed_sha, candidate_sha, project_id=str(project_id)
    )
    if changed is None:
        return {
            "status": "diff_unavailable",
            "eligible": False,
            "changed_files": [],
            "offending_files": [],
            "reason": (
                "could not resolve the candidate diff against the promotion target "
                "(GitHub compare failed)"
            ),
            "candidate_sha": candidate_sha,
            "prior_deployed_sha": prior_deployed_sha,
        }
    verdict = _scope_verdict_for_entries(changed)
    return {
        "status": "eligible" if verdict.eligible else "ineligible",
        "eligible": verdict.eligible,
        "changed_files": [f["filename"] for f in changed],
        "offending_files": verdict.offending_files,
        "reason": verdict.reason,
        "candidate_sha": candidate_sha,
        "prior_deployed_sha": prior_deployed_sha,
    }
