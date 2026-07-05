"""Rollup preflight: assembled-tree module size-cap guard.

Runs in the auto-merge unlocked window alongside the security scan and
Alembic chain guard.  For every changed file in the rollup PR that has a
cap entry in ``backend/testing/module_size_limits.json``, it fetches the
file content at the staging-branch tip, counts lines, and auto-bumps the
cap when the combined tree breaches it.  The bump is committed to the
staging branch so subsequent CI picks up the relaxed limit.

Verdicts (mirroring the Alembic chain guard vocabulary):
  "ok"     — no breach, merge can proceed
  "bumped" — one or more caps auto-bumped and committed; caller defers one
             sweep so the updated JSON propagates to CI
  "hold"   — auto-bump commit failed; caller sets requires_approval
  "defer"  — transient fetch error; caller retries next cycle
"""

import json
import logging

from app.github import commit_file_changes, get_file_text_at_ref, get_pr_files
from app.size_limit_policy import bump_size_limit

logger = logging.getLogger(__name__)

_CAPS_JSON_PATH = "backend/testing/module_size_limits.json"

# Maps each JSON section to the repo-relative prefix that anchors its source
# files, mirroring the resolution in check_size_limits.resolve_section_root.
_SECTION_PREFIXES: dict[str, str] = {
    "backend": "backend/app/",
    "agent": "agent/src/",
    "frontend": "admin-ui/src/",
}


async def preflight_rollup_size_caps(
    gh_token: str,
    owner: str,
    repo: str,
    rollup_pr_number: int,
    staging_branch: str,
    *,
    project_id: str | None = None,
    commit_bump: bool = True,
) -> tuple[str, str | None]:
    """Check assembled-tree module sizes against caps; auto-bump on breach.

    Returns ``(verdict, reason)`` where *verdict* is one of the four strings
    above and *reason* is a human-readable detail string (or ``None`` for
    ``"ok"``).
    """
    # 1. Fetch caps JSON at the staging-branch tip.
    try:
        caps_text = await get_file_text_at_ref(
            _CAPS_JSON_PATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=staging_branch,
            project_id=project_id,
        )
    except Exception as exc:
        return ("defer", f"could not fetch module_size_limits.json: {exc}")

    if caps_text is None:
        return ("defer", "module_size_limits.json not found at staging branch tip")

    try:
        caps_data: dict = json.loads(caps_text)
    except Exception as exc:
        return ("defer", f"could not parse module_size_limits.json: {exc}")

    # 2. List files changed by the rollup PR.
    try:
        pr_files = await get_pr_files(gh_token, owner, repo, rollup_pr_number, project_id)
    except Exception as exc:
        return ("defer", f"could not fetch PR #{rollup_pr_number} files: {exc}")

    # Build status map so we can distinguish deleted files from fetch failures.
    pr_file_status: dict[str, str] = {
        f["filename"]: f.get("status", "")
        for f in pr_files
        if isinstance(f, dict) and "filename" in f
    }
    changed_paths: set[str] = set(pr_file_status)

    # 3. For each capped file that appears in the rollup, fetch content at the
    #    staging-branch tip and check against its cap.
    breaches: list[tuple[str, str, int, int, int]] = []  # (section, key, count, cap, new_cap)
    for section, prefix in _SECTION_PREFIXES.items():
        section_caps = caps_data.get(section)
        if not isinstance(section_caps, dict):
            continue
        for key, cap in section_caps.items():
            if not isinstance(cap, int):
                continue
            full_path = prefix + key
            if full_path not in changed_paths:
                continue
            # Deleted files no longer exist at the staging tip — skip intentionally.
            if pr_file_status.get(full_path) == "removed":
                continue
            try:
                content = await get_file_text_at_ref(
                    full_path,
                    token=gh_token,
                    owner=owner,
                    repo=repo,
                    ref=staging_branch,
                    project_id=project_id,
                )
            except Exception as exc:
                logger.warning(
                    "[ROLLUP-SIZE] PR #%s: could not fetch %s at %s: %s",
                    rollup_pr_number,
                    full_path,
                    staging_branch,
                    exc,
                )
                return ("defer", f"could not fetch {full_path} at {staging_branch}: {exc}")
            if content is None:
                # Fetch returned no content for a non-deleted file — treat as a
                # transient failure and defer rather than silently skipping.
                logger.warning(
                    "[ROLLUP-SIZE] PR #%s: %s returned no content at %s (non-deleted); deferring",
                    rollup_pr_number,
                    full_path,
                    staging_branch,
                )
                return ("defer", f"no content returned for {full_path} at {staging_branch}")
            line_count = len(content.splitlines())
            if line_count > cap:
                new_cap = bump_size_limit(line_count)
                breaches.append((section, key, line_count, cap, new_cap))
                logger.info(
                    "[ROLLUP-SIZE] PR #%s: %s has %d lines (cap %d) — bumping to %d",
                    rollup_pr_number,
                    full_path,
                    line_count,
                    cap,
                    new_cap,
                )

    if not breaches:
        return ("ok", None)

    summary = "; ".join(
        f"{key}: {old_cap} -> {new_cap}"
        for _section, key, _count, old_cap, new_cap in breaches
    )

    if not commit_bump:
        # Caller has exhausted the auto-bump budget — report a hold so the
        # caller can require manual approval without performing another commit.
        logger.warning(
            "[ROLLUP-SIZE] PR #%s: size-cap breach detected but commit_bump=False "
            "(budget exhausted); returning hold: %s",
            rollup_pr_number,
            summary,
        )
        return ("hold", f"auto-bump budget exhausted; breach: {summary}")

    # 4. Write bumped caps into a copy of the parsed JSON and commit.
    updated: dict = dict(caps_data)
    for section, key, _count, _old_cap, new_cap in breaches:
        updated[section] = dict(updated.get(section, {}))
        updated[section][key] = new_cap

    updated_json = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    commit_msg = (
        f"auto-bump module size caps for rollup PR #{rollup_pr_number}: {summary}"
    )

    try:
        await commit_file_changes(
            gh_token,
            owner,
            repo,
            staging_branch,
            upserts={_CAPS_JSON_PATH: updated_json},
            message=commit_msg,
            project_id=project_id,
        )
    except Exception as exc:
        logger.warning(
            "[ROLLUP-SIZE] PR #%s: auto-bump commit failed (%s: %s); holding for approval",
            rollup_pr_number,
            type(exc).__name__,
            exc,
        )
        return ("hold", f"auto-bump commit failed: {exc}")

    logger.info(
        "[ROLLUP-SIZE] PR #%s: committed size-cap bumps on %s: %s",
        rollup_pr_number,
        staging_branch,
        summary,
    )
    return ("bumped", summary)
