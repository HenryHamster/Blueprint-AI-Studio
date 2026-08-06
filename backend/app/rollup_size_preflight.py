"""Rollup preflight: assembled-tree module size-cap guard.

Runs in the auto-merge unlocked window alongside the security scan and
Alembic chain guard.  For every changed file in the rollup PR that has a
cap entry in ``backend/testing/module_size_limits.json``, it fetches the
file content at the staging-branch tip, counts lines, and auto-bumps the
cap when the combined tree breaches it.  The bump is committed to the
staging branch so subsequent CI picks up the relaxed limit.

Verdicts (mirroring the Alembic chain guard vocabulary):
  "ok"     — no breach (or the repo carries no module_size_limits.json at all,
             so no size governance applies here); merge can proceed
  "bumped" — one or more caps auto-bumped and committed; caller defers one
             sweep so the updated JSON propagates to CI
  "hold"   — a chronic offender needs operator decomposition, the auto-bump
             budget is exhausted, or the auto-bump commit failed with a genuine
             (non-retryable) error; caller sets requires_approval / escalates
  "defer"  — a transient condition (fetch error, an unreadable-but-present caps
             file, or a retryable commit failure such as rate-limit 403/429, a
             non-fast-forward 422, or a network blip); caller retries next cycle
"""

import asyncio
import json
import logging
import re

import httpx

from app.github import (
    StaleBranchHeadError,
    commit_file_changes,
    get_branch_sha,
    get_file_content_at_ref,
    get_file_text_at_ref,
    get_pr_files,
)
from app.json_extract import async_json_loads
from app.size_limit_policy import classify_breach, ledger_with_pending_bumps

logger = logging.getLogger(__name__)

_CAPS_JSON_PATH = "backend/testing/module_size_limits.json"
_CAP_BUMP_HISTORY_LEDGER_RELPATH = "backend/testing/cap_bump_history.json"

# HTTP status codes surfaced by a failed auto-bump commit that are transient
# and clear on a later cycle, so they must DEFER (retry) rather than HOLD
# (escalate):
#   403 / 429 — primary / secondary rate limit (the token bucket refills)
#   422       — non-fast-forward ref update (the staging tip moved under us; a
#               fresh read next cycle rebases the bump)
#   408       — request timeout
#   5xx       — GitHub server-side errors
# ``commit_file_changes`` raises RuntimeError whose message embeds "HTTP <code>"
# for any unexpected GitHub status (see github.commit_file_changes._json).
_TRANSIENT_COMMIT_HTTP_CODES = frozenset({403, 408, 422, 429, 500, 502, 503, 504})
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


def _is_transient_commit_error(exc: BaseException) -> bool:
    """Return True when *exc* from the auto-bump commit is transient/retryable.

    ``commit_file_changes`` raises ``RuntimeError`` whose message embeds
    ``HTTP <code>`` for any unexpected GitHub status, and lets
    ``_github_request`` re-raise ``httpx.HTTPError`` for transport-layer
    failures.  Rate-limit (403/429), non-fast-forward (422), 5xx and network
    conditions all clear on a later cycle, so they DEFER (retry) instead of
    HOLD (hard escalation).  Genuinely broken states (a persistent 4xx contract
    error, a code defect) are NOT transient and still HOLD so the operator is
    surfaced rather than the rollup looping forever.
    """
    if isinstance(exc, (httpx.HTTPError, StaleBranchHeadError)):
        return True
    msg = str(exc)
    m = _HTTP_STATUS_RE.search(msg)
    if m is not None and int(m.group(1)) in _TRANSIENT_COMMIT_HTTP_CODES:
        return True
    lowered = msg.lower()
    return any(
        kw in lowered
        for kw in (
            "rate limit",
            "connection",
            "timeout",
            "temporarily unavailable",
            "name resolution",
            "ssl",
        )
    )

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
    # Pin every read to one immutable branch tip. Branch-name content reads are
    # cached for ten minutes and were the direct cause of repeated stale bumps:
    # after the first commit, each poll kept re-reading the old caps document.
    try:
        observed_head_sha = await get_branch_sha(
            gh_token,
            owner,
            repo,
            staging_branch,
            project_id=project_id,
            live=True,
        )
    except Exception as exc:
        return ("defer", f"could not resolve live staging head: {exc}")

    # 1. Fetch caps JSON and changed source at the pinned immutable tip.
    try:
        caps_text = await get_file_text_at_ref(
            _CAPS_JSON_PATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=observed_head_sha,
            project_id=project_id,
        )
    except Exception as exc:
        return ("defer", f"could not fetch module_size_limits.json: {exc}")

    if caps_text is None:
        # ``get_file_text_at_ref`` collapses "file genuinely absent (404)" and
        # "transient read failure" both to None (its contract: None means
        # "could not read", never "empty").  Disambiguate with the tri-state
        # existence probe so a repo that simply doesn't use this governance file
        # is NEVER livelocked (proceed), while a transient read blip still
        # DEFERS — a real breach must not slip through on a GitHub hiccup.
        exists, _ = await get_file_content_at_ref(
            _CAPS_JSON_PATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=observed_head_sha,
            project_id=project_id,
        )
        if exists is False:
            logger.info(
                "[ROLLUP-SIZE] PR #%s: no %s at %s — repo has no module size "
                "governance; proceeding",
                rollup_pr_number,
                _CAPS_JSON_PATH,
                staging_branch,
            )
            return ("ok", None)
        # exists is True (present but text unreadable) or None (indeterminate):
        # both are transient — retry next cycle rather than merge unchecked.
        return (
            "defer",
            "module_size_limits.json present but unreadable at staging branch tip",
        )

    try:
        caps_data: dict = await async_json_loads(caps_text)
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
    # Chronic offenders: (section, key, bump_count) — must hold without bumping.
    chronic_holders: list[tuple[str, str, int]] = []
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
                    ref=observed_head_sha,
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
                verdict = await asyncio.to_thread(
                    classify_breach,
                    module_key=key, current_size=line_count, section=section
                )
                if verdict["is_chronic"] and verdict["suggested_new_limit"] is None:
                    chronic_holders.append((section, key, verdict["bump_count"]))
                    logger.warning(
                        "[ROLLUP-SIZE] PR #%s: %s is a chronic offender (%d bumps)"
                        " — holding for operator decomposition",
                        rollup_pr_number,
                        full_path,
                        verdict["bump_count"],
                    )
                else:
                    new_cap = verdict["suggested_new_limit"]
                    breaches.append((section, key, line_count, cap, new_cap))
                    logger.info(
                        "[ROLLUP-SIZE] PR #%s: %s has %d lines (cap %d) — bumping to %d",
                        rollup_pr_number,
                        full_path,
                        line_count,
                        cap,
                        new_cap,
                    )

    # Verify the PR branch did not move while we assembled the verdict. This
    # protects every outcome (including "ok") from authorizing a merge based on
    # a stale tree; the write path below also enforces the same head as a CAS.
    try:
        current_head_sha = await get_branch_sha(
            gh_token,
            owner,
            repo,
            staging_branch,
            project_id=project_id,
            live=True,
        )
    except Exception as exc:
        return ("defer", f"could not revalidate live staging head: {exc}")
    if current_head_sha != observed_head_sha:
        return (
            "defer",
            "staging branch advanced during size-cap preflight "
            f"({observed_head_sha[:12]} -> {current_head_sha[:12]})",
        )

    # Any chronic offender blocks ALL auto-bumps — never partially commit caps
    # while an operator decomposition decision is pending.
    if chronic_holders:
        names = ", ".join(
            f"{key} ({count} bumps)" for _, key, count in chronic_holders
        )
        logger.warning(
            "[ROLLUP-SIZE] PR #%s: holding for operator — chronic offender(s): %s",
            rollup_pr_number,
            names,
        )
        return ("hold", f"chronic offender(s) require decomposition: {names}")

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

    # Keep the committed cap-bump-history ledger in sync: the deployed backend
    # reads the ledger (not git), so an auto-bump must be recorded there too or
    # production would classify from a stale history. A repo whose staging
    # branch has no ledger yet (pre-feature baseline) simply skips the ledger
    # write — a partial ledger with only pending records would be worse. A
    # transient read failure defers the whole bump so the mirror never goes
    # stale silently.
    upserts: dict[str, str] = {_CAPS_JSON_PATH: updated_json}
    try:
        ledger_text = await get_file_text_at_ref(
            _CAP_BUMP_HISTORY_LEDGER_RELPATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=observed_head_sha,
            project_id=project_id,
        )
    except Exception as exc:
        return (
            "defer",
            f"could not fetch cap-bump-history ledger before auto-bump commit: {exc}",
        )
    if ledger_text is not None:
        try:
            ledger_data = json.loads(ledger_text)
        except Exception as exc:
            return ("defer", f"could not parse cap-bump-history ledger: {exc}")
        pending = [
            (section, key, cap, new_cap)
            for section, key, _count, cap, new_cap in breaches
        ]
        ledger_data = ledger_with_pending_bumps(ledger_data, pending)
        upserts[_CAP_BUMP_HISTORY_LEDGER_RELPATH] = (
            json.dumps(ledger_data, indent=2, ensure_ascii=False) + "\n"
        )
        commit_msg += "; record bumps in cap-bump-history ledger"

    try:
        committed_sha = await commit_file_changes(
            gh_token,
            owner,
            repo,
            staging_branch,
            upserts=upserts,
            message=commit_msg,
            project_id=project_id,
            expected_head_sha=observed_head_sha,
        )
    except Exception as exc:
        # A transient/retryable commit failure (rate-limit 403/429, a
        # non-fast-forward 422 from a moved staging tip, a 5xx, or a network
        # blip) is NOT a reason to hard-escalate — the next cycle re-reads the
        # tip and re-commits.  Only a genuine, non-retryable error HOLDs.
        if _is_transient_commit_error(exc):
            logger.warning(
                "[ROLLUP-SIZE] PR #%s: auto-bump commit failed transiently "
                "(%s: %s); deferring to next cycle",
                rollup_pr_number,
                type(exc).__name__,
                exc,
            )
            return ("defer", f"auto-bump commit failed (transient): {exc}")
        logger.warning(
            "[ROLLUP-SIZE] PR #%s: auto-bump commit failed (%s: %s); holding for approval",
            rollup_pr_number,
            type(exc).__name__,
            exc,
        )
        return ("hold", f"auto-bump commit failed: {exc}")

    if committed_sha == observed_head_sha:
        logger.info(
            "[ROLLUP-SIZE] PR #%s: size-cap update was already present on %s; "
            "no commit created",
            rollup_pr_number,
            staging_branch,
        )
        return ("ok", None)

    logger.info(
        "[ROLLUP-SIZE] PR #%s: committed size-cap bumps on %s: %s",
        rollup_pr_number,
        staging_branch,
        summary,
    )
    return ("bumped", summary)
